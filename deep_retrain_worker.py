"""
deep_retrain_worker.py — изолированный сабпроцесс глубокого переобучения.

Восстанавливает модели, убранные из "горячего" процесса ради экономии RAM
на маломощных хостах (Bothost): HistGradientBoosting, XGBoost, LightGBM (если
установлен), MLP. Работает СТРОГО через базу данных:

  1. Читает полную историю обучающих примеров из bot_ai_examples (без лимита
     живого RAM-буфера основного процесса).
  2. Обучает тяжёлый ансамбль в СВОЁМ ОТДЕЛЬНОМ процессе — импорт xgboost/
     lightgbm/sklearn здесь никогда не увеличивает RSS основного gunicorn-
     воркера, потому что это другой процесс ОС.
  3. Сохраняет обученные модели (pickle) обратно в БД (bot_ai_deep_models).
  4. Завершается — вся память процесса возвращается ОС полностью (в отличие
     от gc.collect()/malloc_trim в долгоживущем процессе, которые не всегда
     отдают память ОС).

Основной торговый процесс НИКОГДА не импортирует xgboost/lightgbm сам —
он лишь опционально подгружает уже готовые pickle-блобы из БД (см.
ai_engine.py: load_deep_models()), и то только когда это разрешено
(LOW_MEMORY_MODE=0, т.е. хост с подтверждённым запасом RAM).

Запускается из trader.py как `subprocess.Popen([sys.executable, __file__])`
раз в 2 дня — не блокирует и не делит память с основным процессом.
"""

import io
import logging
import pickle
import sys

import numpy as np

logging.basicConfig(level=logging.INFO, format="[deep-retrain] %(message)s")
log = logging.getLogger(__name__)

WINDOW = 3000          # сколько последних примеров тянуть из БД
TEST_FRACTION = 0.15   # доля на holdout для оценки accuracy


def _split(X, y, w):
    n = len(X)
    n_test = max(1, int(n * TEST_FRACTION))
    idx = np.arange(n)
    rng = np.random.RandomState(42)
    rng.shuffle(idx)
    test_idx, train_idx = idx[:n_test], idx[n_test:]
    return (X[train_idx], y[train_idx], w[train_idx],
            X[test_idx], y[test_idx], w[test_idx])


def main():
    import db_store
    # Если пул не поднялся при импорте (timeout на pghost.ru) — пробуем ещё раз.
    # Сабпроцесс запускается редко (раз в 2 дня), поэтому можем позволить
    # дополнительные 60с ожидания: 3 попытки × ~20с.
    if not db_store._available:
        import time as _time
        log.warning("БД не подключилась при импорте — пробуем переподключиться (до 3 раз)")
        for attempt in range(1, 4):
            _time.sleep(attempt * 10)   # 10s, 20s, 30s
            try:
                with db_store._pool_lock:
                    db_store._init_pool()
            except Exception as e:
                log.warning(f"Попытка {attempt}/3: {e}")
            if db_store._available:
                log.info("Переподключение успешно")
                break
        else:
            log.error("БД недоступна после 3 попыток — выходим")
            return 1

    examples = db_store.ai_examples_get_recent(WINDOW)
    if len(examples) < 30:
        log.info(f"Недостаточно примеров в БД ({len(examples)}) — пропуск")
        return 0

    X = np.array([e["features"] for e in examples], dtype=float)
    y = np.array([e["label"] for e in examples], dtype=int)
    w = np.array([e["weight"] for e in examples], dtype=float)
    w = w / (w.mean() + 1e-10)

    if len(np.unique(y)) < 2:
        log.info("Недостаточно классов в данных — пропуск")
        return 0

    X_tr, y_tr, w_tr, X_te, y_te, w_te = _split(X, y, w)
    n_examples = len(examples)

    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.neural_network import MLPClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.metrics import accuracy_score

    models = {}

    models["HGB"] = HistGradientBoostingClassifier(
        max_iter=200, max_depth=6, learning_rate=0.06, random_state=42)

    try:
        from xgboost import XGBClassifier
        models["XGB"] = XGBClassifier(
            n_estimators=300, max_depth=5, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            objective="multi:softprob", num_class=3,
            eval_metric="mlogloss", tree_method="hist",
            n_jobs=2, random_state=42)
    except Exception as e:
        log.info(f"XGBoost недоступен: {e}")

    try:
        from lightgbm import LGBMClassifier
        models["LGB"] = LGBMClassifier(
            n_estimators=400, max_depth=6, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            n_jobs=2, random_state=42, verbosity=-1)
    except Exception as e:
        log.info(f"LightGBM недоступен: {e}")

    models["MLP"] = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", MLPClassifier(
            hidden_layer_sizes=(128, 64, 32), max_iter=300,
            early_stopping=True, random_state=42)),
    ])

    # XGBClassifier ожидает классы 0..N-1, а не {-1,0,1} — перекодируем
    classes_sorted = sorted(np.unique(y_tr).tolist())
    remap = {c: i for i, c in enumerate(classes_sorted)}
    y_tr_enc = np.array([remap[v] for v in y_tr])
    y_te_enc = np.array([remap[v] for v in y_te])

    saved = 0
    for name, model in models.items():
        try:
            if name in ("XGB",):
                model.fit(X_tr, y_tr_enc, sample_weight=w_tr)
                pred = model.predict(X_te)
                acc = accuracy_score(y_te_enc, pred)
            else:
                model.fit(X_tr, y_tr, sample_weight=w_tr)
                pred = model.predict(X_te)
                acc = accuracy_score(y_te, pred)

            buf = io.BytesIO()
            pickle.dump({"model": model, "classes_sorted": classes_sorted,
                         "uses_remap": name == "XGB"}, buf)
            db_store.deep_model_save(name, buf.getvalue(), float(acc), n_examples)
            log.info(f"{name}: accuracy={acc:.3f} на {n_examples} примерах — сохранено в БД")
            saved += 1
        except Exception as e:
            log.warning(f"{name}: ошибка обучения/сохранения — {e}")

    log.info(f"Готово: {saved}/{len(models)} тяжёлых моделей обновлены в БД")
    return 0


if __name__ == "__main__":
    sys.exit(main())
