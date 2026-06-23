export function NeuralPanel() {
  const probUp   = 68;
  const probHold = 20;
  const probDown = 12;
  const fiData = [
    { name: "rsi",          val: 22.4 },
    { name: "macd_h",       val: 18.1 },
    { name: "ema_cross",    val: 14.7 },
    { name: "bb_pos",       val: 12.3 },
    { name: "vol_ratio",    val: 10.9 },
    { name: "ret_5",        val: 8.6  },
    { name: "stoch_k",      val: 7.2  },
    { name: "momentum",     val: 5.8  },
  ];
  const maxFi = fiData[0].val;

  return (
    <div style={{
      background:"#0d0f14", minHeight:"100vh", padding:"24px",
      fontFamily:"'Segoe UI',system-ui,sans-serif", color:"#e2e8f0"
    }}>
      {/* Header */}
      <div style={{display:"flex",alignItems:"center",gap:"12px",marginBottom:"24px"}}>
        <span style={{fontSize:"22px",fontWeight:800,color:"#00d4aa",letterSpacing:"1px"}}>🤖 GRINCH-GRAM</span>
        <span style={{background:"#0d2e22",color:"#00d4aa",padding:"3px 10px",borderRadius:"4px",fontSize:"11px",fontWeight:700}}>AI ENGINE v2</span>
        <span style={{background:"#3b3228",color:"#ffd166",padding:"3px 10px",borderRadius:"4px",fontSize:"11px",fontWeight:700}}>ДЕМО</span>
      </div>

      <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:"16px"}}>

        {/* AI Brain */}
        <div style={{
          background:"linear-gradient(135deg,#141720,#141a2e)",
          border:"1px solid #2a3a5c",borderRadius:"12px",padding:"18px"
        }}>
          <div style={{fontSize:"13px",fontWeight:700,color:"#8892b0",marginBottom:"14px"}}>
            🧠 AI Мозг
            <span style={{background:"#0d2e22",color:"#00d4aa",padding:"2px 8px",
              borderRadius:"4px",fontSize:"10px",marginLeft:"8px"}}>
              ✓ Обучена (94 баров)
            </span>
          </div>

          {/* AI Signals row */}
          <div style={{display:"flex",gap:"8px",marginBottom:"16px"}}>
            <div style={{flex:1,background:"#0d2e22",border:"1px solid #00d4aa40",
              borderRadius:"8px",padding:"10px",textAlign:"center"}}>
              <div style={{fontSize:"18px",fontWeight:800,color:"#00d4aa"}}>BUY</div>
              <div style={{fontSize:"10px",color:"#8892b0",marginTop:"2px"}}>AI 68%</div>
            </div>
            <div style={{flex:1,background:"#0d2e22",border:"1px solid #00d4aa40",
              borderRadius:"8px",padding:"10px",textAlign:"center"}}>
              <div style={{fontSize:"18px",fontWeight:800,color:"#00d4aa"}}>BUY</div>
              <div style={{fontSize:"10px",color:"#8892b0",marginTop:"2px"}}>42% Tech</div>
            </div>
          </div>

          {/* Probability bars */}
          {[
            {label:"▲ Рост",   val:probUp,   color:"#00d4aa"},
            {label:"◆ Боковик",val:probHold, color:"#ffd166"},
            {label:"▼ Падение",val:probDown, color:"#ff4d6d"},
          ].map(row => (
            <div key={row.label} style={{display:"flex",alignItems:"center",gap:"8px",marginBottom:"8px"}}>
              <span style={{fontSize:"11px",color:"#8892b0",width:"70px",flexShrink:0}}>{row.label}</span>
              <div style={{flex:1,background:"#1c2030",borderRadius:"3px",height:"10px",overflow:"hidden"}}>
                <div style={{width:row.val+"%",height:"100%",background:row.color,borderRadius:"3px",transition:"width .6s"}}/>
              </div>
              <span style={{fontSize:"11px",fontFamily:"monospace",fontWeight:700,color:row.color,width:"34px",textAlign:"right"}}>{row.val}%</span>
            </div>
          ))}

          {/* Regime */}
          <div style={{
            marginTop:"12px",padding:"6px 12px",borderRadius:"6px",
            border:"1px solid #00d4aa",color:"#00d4aa",fontWeight:700,fontSize:"12px",textAlign:"center"
          }}>
            UPTREND — Восходящий тренд
          </div>
        </div>

        {/* Forecast + S/R */}
        <div style={{display:"flex",flexDirection:"column",gap:"16px"}}>
          {/* Forecast */}
          <div style={{background:"#141720",border:"1px solid #2a2f45",borderRadius:"12px",padding:"18px"}}>
            <div style={{fontSize:"13px",fontWeight:700,color:"#8892b0",marginBottom:"12px"}}>🔮 Прогноз цены (AI)</div>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr 1fr",gap:"8px",marginBottom:"8px"}}>
              {[["$67,204","1 св"],["$67,408","2 св"],["$67,612","3 св"]].map(([p,l]) => (
                <div key={l} style={{background:"#1c2030",borderRadius:"6px",padding:"10px",textAlign:"center"}}>
                  <div style={{fontSize:"10px",color:"#8892b0",marginBottom:"4px"}}>{l}</div>
                  <div style={{fontSize:"14px",fontWeight:700,fontFamily:"monospace",color:"#00d4aa"}}>{p}</div>
                </div>
              ))}
            </div>
            <div style={{fontSize:"11px",color:"#8892b0",textAlign:"center"}}>
              Диапазон: $65,820 – $68,240 (ATR)
            </div>
          </div>

          {/* S/R */}
          <div style={{background:"#141720",border:"1px solid #2a2f45",borderRadius:"12px",padding:"18px",flex:1}}>
            <div style={{fontSize:"13px",fontWeight:700,color:"#8892b0",marginBottom:"12px"}}>📐 Уровни S/R</div>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:"12px"}}>
              <div>
                <div style={{fontSize:"11px",fontWeight:700,color:"#ff4d6d",marginBottom:"6px"}}>🔴 Сопротивление</div>
                {["$68,420","$67,950","$67,580"].map(v => (
                  <div key={v} style={{fontSize:"12px",fontFamily:"monospace",padding:"4px 8px",borderRadius:"4px",
                    background:"#2e152020",border:"1px solid #ff4d6d50",color:"#ff4d6d",textAlign:"center",marginBottom:"4px"}}>{v}</div>
                ))}
              </div>
              <div>
                <div style={{fontSize:"11px",fontWeight:700,color:"#00d4aa",marginBottom:"6px"}}>🟢 Поддержка</div>
                {["$66,120","$65,740","$65,200"].map(v => (
                  <div key={v} style={{fontSize:"12px",fontFamily:"monospace",padding:"4px 8px",borderRadius:"4px",
                    background:"#0d2e2220",border:"1px solid #00d4aa50",color:"#00d4aa",textAlign:"center",marginBottom:"4px"}}>{v}</div>
                ))}
              </div>
            </div>
          </div>
        </div>

        {/* Feature Importance + Patterns */}
        <div style={{display:"flex",flexDirection:"column",gap:"16px"}}>
          {/* Feature importance */}
          <div style={{background:"#141720",border:"1px solid #2a2f45",borderRadius:"12px",padding:"18px"}}>
            <div style={{fontSize:"13px",fontWeight:700,color:"#8892b0",marginBottom:"12px"}}>🏆 Важность признаков (RF)</div>
            {fiData.map(f => (
              <div key={f.name} style={{display:"flex",alignItems:"center",gap:"8px",marginBottom:"6px"}}>
                <span style={{fontSize:"10px",color:"#8892b0",fontFamily:"monospace",width:"80px",flexShrink:0}}>{f.name}</span>
                <div style={{flex:1,background:"#1c2030",borderRadius:"3px",height:"8px",overflow:"hidden"}}>
                  <div style={{width:(f.val/maxFi*100).toFixed(0)+"%",height:"100%",
                    background:"linear-gradient(90deg,#a78bfa,#4f8ef7)",borderRadius:"3px"}}/>
                </div>
                <span style={{fontSize:"10px",fontFamily:"monospace",color:"#a78bfa",width:"34px",textAlign:"right"}}>{f.val}%</span>
              </div>
            ))}
          </div>

          {/* Candle patterns */}
          <div style={{background:"#141720",border:"1px solid #2a2f45",borderRadius:"12px",padding:"18px",flex:1}}>
            <div style={{fontSize:"13px",fontWeight:700,color:"#8892b0",marginBottom:"12px"}}>🕯️ Паттерны свечей</div>
            {[
              {icon:"🟢",name:"Бычье поглощение",desc:"Сильный сигнал вверх",color:"#00d4aa"},
              {icon:"🟢",name:"Три белых солдата",desc:"Тренд вверх",color:"#00d4aa"},
              {icon:"🟡",name:"Дожи",desc:"Нерешительность рынка",color:"#ffd166"},
            ].map(p => (
              <div key={p.name} style={{
                fontSize:"12px",padding:"7px 10px",borderRadius:"5px",
                borderLeft:`3px solid ${p.color}`,background:p.color+"15",marginBottom:"6px"
              }}>
                {p.icon} <b>{p.name}</b> — <span style={{color:"#8892b0"}}>{p.desc}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Price + stats footer */}
      <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:"12px",marginTop:"16px"}}>
        {[
          {label:"Цена BTC",val:"$66,987","color":"#00d4aa"},
          {label:"RSI",val:"62.4",color:"#e2e8f0"},
          {label:"Winrate",val:"68%",color:"#00d4aa"},
          {label:"PNL",val:"+$342.5",color:"#00d4aa"},
          {label:"Сделок",val:"12",color:"#e2e8f0"},
        ].map(s => (
          <div key={s.label} style={{background:"#141720",border:"1px solid #2a2f45",
            borderRadius:"8px",padding:"12px",textAlign:"center"}}>
            <div style={{fontSize:"11px",color:"#8892b0",marginBottom:"4px"}}>{s.label}</div>
            <div style={{fontSize:"18px",fontWeight:800,color:s.color,fontFamily:"monospace"}}>{s.val}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
