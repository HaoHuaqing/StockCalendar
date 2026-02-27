# 财报与宏观发布日历

一个基于日历视图的网站，定时抓取并展示：
- 股票财报相关事件（由使用者自行配置关注股票）
- 宏观数据发布事件（就业、PMI、CPI、住宅价格相关）

点击事件会跳转到对应消息来源。

## 功能

- 月/周/列表视图切换
- 事件按 `财报 / 宏观` 与 `A股 / 港股 / 美股` 过滤
- 后端每 6 小时自动刷新
- 支持手动刷新：`POST /api/refresh`

## 数据源

- 股票事件：东方财富公告接口 + A股预约披露日历
  - `https://np-anotice-stock.eastmoney.com/api/security/ann`
  - `https://datacenter-web.eastmoney.com/api/data/v1/get` (`RPT_STOCKCALENDAR`)
- 宏观事件：东方财富全球财经快讯（经济数据相关栏目）
  - `https://np-weblist.eastmoney.com/comm/web/getFastNewsList`

## 运行

```bash
cd /root/calendar
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

浏览器访问：`http://localhost:8000`

## API

- `GET /api/events`：获取事件
  - 可选参数：`start=YYYY-MM-DD&end=YYYY-MM-DD`
- `GET /api/status`：获取缓存状态和统计
- `POST /api/refresh`：手动触发抓取

## 股票配置（本地）

仓库默认不包含个人关注股票代码。使用者需自行在本地配置：

```bash
cp stocks.example.json stocks.json
```

然后可以通过页面“股票配置”面板新增并保存你自己的关注列表。  
`stocks.json` 已在 `.gitignore` 中，避免提交个人配置。

说明：
- 支持只填“名称”或“代码”，保存时会自动识别补全。
- 也支持聚宽风格代码（自动识别市场），例如：
  - `XXXXXX.XSHE`、`XXXXXX.XSHG`、`XXXXX.XHKG`、`TICKER.US`

## 服务器部署（systemd）

项目已提供一个可直接使用的 systemd 服务示例（监听 80 端口）：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now market-calendar.service
sudo systemctl status market-calendar.service
```
