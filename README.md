# 星穹铁道 · 终局玩法血量膨胀分析

追踪崩坏：星穹铁道四种终局玩法（混沌回忆 / 虚构叙事 / 末日幻影 / 异相仲裁）各正式服版本最高难度的血量，提供膨胀曲线、环比膨胀率、相对基准倍数、指数/线性拟合、拐点检测与膨胀加速度分析。

数据来源：[nanoka.cc](https://hsr.nanoka.cc/maze/)（静态 JSON，仅抓取正式服版本)

## 目录结构

```
hsr-hp-inflation/
├── index.html                     # 前端页面（ECharts + 数学分析 + 高清导出）
├── data/
│   ├── health_data.json           # 爬虫输出（结构化数据）
│   ├── health_data.js             # 同内容 JS 版本（页面加载用）
│   └── overrides.json             # 手动修正覆盖层（与自动抓取分离）
├── scripts/
│   ├── scraper.py                 # 抓取 + 血量计算 + 安全写入
│   └── validate.py                # 数据完整性校验（防止坏数据上线）
└── .github/workflows/scrape.yml   # 每周定时抓取（GitHub Actions）

