# 使用统计

皮肤与插件共用的安装统计页面，默认中文和城市层级，支持国家、省／州、城市及产品、版本汇总。

[国内入口](https://home.zhouchenxing.com/usage-stats/) · [GitHub Pages](https://chowjw-vibecode.github.io/usage-stats/)

页面只读取公开汇总，不公开 IP、安装编号或单条安装记录。旧记录省市显示待更新，等待客户端下次真实上报。位置反映 IP 网络出口，代理、VPN 和定位库误差会影响结果。

本地验证：`node tests/versions.cjs` 和 `node tests/cities.cjs`（已安装 Playwright 时）。城市测试使用合成数据并拦截全部外部请求，不请求生产 API。后台源码在 `service/`，严格公开汇总导出器在 `tools/`，旧入口兼容转发在 `relay/`。

服务私库存储每个安装最新有效公网 IP、IP 采集时间、对应国家省市，以及编号摘要、产品、版本和首次／最近上报时间。不保存 IP 历史；没有有效 IP 的旧请求保留最近有效位置。公开接口和快照仅含汇总。本地 City Lite 查询不向第三方定位 API 发送 IP；地理数据来自 [DB-IP](https://db-ip.com/)（CC BY 4.0）。

后端检查：在 `service/` 运行 `python3 -m unittest test_server.py`；根目录运行 `python3 tests/export.test.py` 和 `node --test relay/tests/relay.test.mjs`。生产部署需要原有本机环境变量、City MMDB 和仅监听回环地址的 Nginx 代理；仓库不含数据、令牌或运行环境文件。

## 每杯研究记录

`POST /usage-stats/api/v1/cup-events` 接收 OneCup 已完成杯次及已保存评价事件，最多 4 MiB 未压缩 JSON，读取超时 30 秒；原安装统计接口继续兼容。事件字段为 `schema:1`、`event_id`、`install_id`、`product:this_cup`、`shot_id`、`event_type`、`occurred_at`、`payload`。payload 允许 profile、workflow、measurements、result、bean、taste、device、versions。

杯记录按事件编号幂等追加，相同编号不同内容返回 409。数据库使用 gzip BLOB 无损保存全部分析字段；同杯评价修改追加新事件，不覆盖旧评价。`cup_events` 的 UPDATE/DELETE 有拒绝触发器，不设置自动过期；安装记录删除不级联杯记录。每个事件保存自己的接收时间、来源 IP 与省市快照，安装摘要仍只保留最新有效 IP。

固定 `device.machine_id` 从硬件序列号在客户端本地散列生成，仅区分机器，不关联人，明文序列号与无关凭据不允许上传。读取不到序列号则为 null。旧杯的制作版本保留在事件中，不用于倒退安装摘要的当前版本。公开页面和汇总 API 不返回原始杯记录、机器编号或 IP。私有分析可在服务器本地使用 `Store.read_cup_event(event_id)` 还原压缩数据。

生产验证只读取状态和结构，禁止插入合成研究杯后再清理。写入与重传测试使用临时 SQLite 数据库。
