# 使用统计

皮肤与插件共用的安装统计页面，默认中文和城市层级，支持国家、省／州、城市及产品、版本汇总。

[国内入口](https://home.zhouchenxing.com/usage-stats/) · [GitHub Pages](https://chowjw-vibecode.github.io/usage-stats/)

页面只读取公开汇总，不公开 IP、安装编号或单条安装记录。旧记录省市显示待更新，等待客户端下次真实上报。位置反映 IP 网络出口，代理、VPN 和定位库误差会影响结果。

本地验证：`node tests/versions.cjs` 和 `node tests/cities.cjs`（已安装 Playwright 时）。城市测试使用合成数据并拦截全部外部请求，不请求生产 API。后台源码在 `service/`，严格公开汇总导出器在 `tools/`，旧入口兼容转发在 `relay/`。

服务私库存储每个安装最新有效公网 IP、IP 采集时间、对应国家省市，以及编号摘要、产品、版本和首次／最近上报时间。不保存 IP 历史；没有有效 IP 的旧请求保留最近有效位置。公开接口和快照仅含汇总。本地 City Lite 查询不向第三方定位 API 发送 IP；地理数据来自 [DB-IP](https://db-ip.com/)（CC BY 4.0）。

后端检查：在 `service/` 运行 `python3 -m unittest test_server.py`；根目录运行 `python3 tests/export.test.py` 和 `node --test relay/tests/relay.test.mjs`。生产部署需要原有本机环境变量、City MMDB 和仅监听回环地址的 Nginx 代理；仓库不含数据、令牌或运行环境文件。
