"""
deep_retrain_worker.py — изолированный сабпроцесс глубокого переобучения.

Восстанавливает модели, убранные из "горячего" процесса ради экономии RAM
на маломощных хостах (Bothost): HistGradientBoosting, XGBoost, LightGBM (если
установлен), MLP. Работает СТРОГО через базу данных:

  1. Читает полную историю из bot_ai_examples (без лимита оперативного буфера).
  2. Обучает тяжёлый ансамбль в СВОЁМ ОТДЕЛЬНОМ процессе — импорт xgboost/
     lightgbm/sklearn здесь никогда не увеличивает RSS основного gunicorn-
     воркера, потому что это другой процесс ОС.
  3. Сохраняет обученные модели (pickle) обратно в БД (bot_ai_deep_models).
  4. Завершается — вся память возвращается ОС полностью.

Stdout-маркер для родительского процесса:
  RESULT:SKIPPED        — нечего обучать (мало данных / БД недоступна)
  RESULT:SAVED:N        — обучено и сохранено N моделей
"""

import io
import logging
import pickle
import sys

import numpy as np

logging.basicConfig(level=logging.INFO, format="[deep-retrain] %(message)s")
log = logging.getLogger(__name__)

WINDOW        = 3000
TEST_FRACTION = 0.15
MIN_EXAMPLES  = 30


def _split(X, y, w):
    n      = len(X)
    n_test = max(1, int(n * TEST_FRACTION))
    idx    = np.arange(n)
    np.random.RandomState(42).shuffle(idx)
    test_idx, train_idx = idx[:n_test], idx[n_test:]
    return (X[train_idx], y[train_idx], w[train_idx],
            X[test_idx],  y[test_idx],  w[test_idx])


def _result(kind, n=0):
    """Печатает маркер RESULT:... и возвращает код выхода."""
    if kind == "skipped":
        print("RESULT:SKIPPED", flush=True)
        return 0
    print(f"RESULT:SAVED:{n}", flush=True)
    return 0


def main():
    import db_store

    if not db_store._available:
        log.error("БД недоступна — сабпроцесс завершается без обучения")
        return _result("skipped")

    examples = db_store.ai_examples_get_recent(WINDOW)
    n_ex = len(examples)
    if n_ex < MIN_EXAMPLES:
        log.info(
            f"Примеров в БД: {n_ex} (нужно ≥{MIN_EXAMPLES}) — "
            f"обучение отложено до накопления данных"
        )
        return _result("skipped")

    X = np.array([e["features"] for e in examples], dtype=float)
    y = np.array([e["label"]    for e in examples], dtype=int)
    w = np.array([e["weight"]   for e in examples], dtype=float)
    w = w / (w.mean() + 1e-10)

    if len(np.unique(y)) < 2:
        log.info("Только один класс в данных — обучение отложено")
        return _result("skipped")

    X_tr, y_tr, w_tr, X_te, y_te, w_te = _split(X, y, w)

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
        ("clf",    MLPClassifier(
            hidden_layer_sizes=(128, 64, 32), max_iter=300,
            early_stopping=True, random_state=42)),
    ])

    # XGBClassifier ожидает 0..N-1, а не {-1,0,1}
    classes_sorted = sorted(np.unique(y_tr).tolist())
    remap    = {c: i for i, c in enumerate(classes_sorted)}
    y_tr_enc = np.array([remap[v] for v in y_tr])
    y_te_enc = np.array([remap[v] for v in y_te])

    saved = 0
    for name, model in models.items():
        try:
            if name == "XGB":
                model.fit(X_tr, y_tr_enc, sample_weight=w_tr)
                acc = accuracy_score(y_te_enc, model.predict(X_te))
            else:
                model.fit(X_tr, y_tr, sample_weight=w_tr)
                acc = accuracy_score(y_te, model.predict(X_te))

            buf = io.BytesIO()
            pickle.dump({"model": model, "classes_sorted": classes_sorted,
                         "uses_remap": name == "XGB"}, buf)
            db_store.deep_model_save(name, buf.getvalue(), float(acc), n_ex)
            log.info(f"{name}: acc={acc:.3f} на {n_ex} примерах — сохранено в БД")
            saved += 1
        except Exception as e:
            log.warning(f"{name}: ошибка обучения/сохранения — {e}")

    log.info(f"Итого: {saved}/{len(models)} тяжёлых моделей обновлены в БД")
    return _result("saved", saved)


if __name__ == "__main__":
    sys.exit(main())
