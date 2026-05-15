# Baidu Ecommerce Search

百度电商一站式服务，覆盖商品知识查询和购物交易全流程。支持商品对比、品牌知识、品类选购指南、商品参数解读、品牌榜单及单品榜单等知识查询能力；同时提供商品搜索、规格查看、地址管理、下单购买、订单查询及售后服务等完整交易链路，帮助用户从决策到购买一步到位。

## Setup

环境依赖：Python 3.x，仅使用标准库，无需安装第三方包。

配置步骤：

1. 访问 https://openai.baidu.com 登录百度账号，点击权限申请勾选所需能力。
2. 设置环境变量：

```bash
export BAIDU_EC_SEARCH_TOKEN="your-token"
export BAIDU_EC_SEARCH_QPS="1"
```

`BAIDU_EC_SEARCH_QPS` 可选，默认 1，设为 0 表示无限制。

## Runtime Files

完整 OpenClaw 安装应包含以下脚本。本地 agent 只有在这些脚本存在时才能执行对应能力：

- `scripts/compare.py`
- `scripts/knowledge.py`
- `scripts/ranking.py`
- `scripts/spu.py`
- `scripts/order.py`
- `scripts/after_service.py`
- `scripts/address.py`

在当前项目中，脚本已安装到 `skills/baidu-ecommerce-search/scripts/`。通过 `run_command` 从项目根目录执行时，使用 `python skills/baidu-ecommerce-search/scripts/<script>.py ...`。如果当前工作目录已经切换到 skill 目录内部，才使用原始的 `python3 scripts/<script>.py ...` 形式。

## 全局交互规范

简化用户输入。

展示列表时必须带序号供用户输入序号选择，确认环节告知用户可输入 `1` 或 `确认`。

所有可跳转内容用 `[文本](URL)` 格式，URL 中的 `|` 必须转义为 `\|`，优先使用接口返回的购买链接。

## 能力清单

以下能力是可组合的工具箱。响应用户时，先分析哪些能力与用户问题相关，再调用所有相关能力。

### 电商知识

- 商品对比：参数、口碑、价格全方位对比，仅支持两个对比。命令：`python skills/baidu-ecommerce-search/scripts/compare.py "<对比查询>"`
- 品牌知识：品牌简介、定位、明星产品、大事记。命令：`python skills/baidu-ecommerce-search/scripts/knowledge.py brand "<品牌名>"`
- 品类知识：品类选购要点、避坑指南。命令：`python skills/baidu-ecommerce-search/scripts/knowledge.py entity "<品类名>怎么选"`
- 商品参数：单品规格参数及 AI 解读。命令：`python skills/baidu-ecommerce-search/scripts/knowledge.py param "<商品名>"`
- 品牌榜单：某品类下的品牌排行。命令：`python skills/baidu-ecommerce-search/scripts/ranking.py brand "<榜单查询>"`
- 单品榜单：某品牌下的商品排行。命令：`python skills/baidu-ecommerce-search/scripts/ranking.py product "<榜单查询>"`

### 百度优选

- 商品搜索：搜索可直接下单的商品。命令：`python skills/baidu-ecommerce-search/scripts/spu.py list "<关键词>"`
- 商品详情：获取 SKU 规格及价格。命令：`python skills/baidu-ecommerce-search/scripts/spu.py detail <spuId>`
- 创建订单：`python skills/baidu-ecommerce-search/scripts/order.py create --sku-id <skuId> --spu-id <spuId> --addr-id <addrId>`
- 订单历史：`python skills/baidu-ecommerce-search/scripts/order.py history`
- 订单详情：`python skills/baidu-ecommerce-search/scripts/order.py detail <orderId>`
- 售后查询：`python skills/baidu-ecommerce-search/scripts/after_service.py <orderId>`
- 地址列表：`python skills/baidu-ecommerce-search/scripts/address.py list`
- 地址识别：从自然语言提取结构化地址。命令：`python skills/baidu-ecommerce-search/scripts/address.py recognise "<姓名 地址 手机号>"`
- 地址添加：`python skills/baidu-ecommerce-search/scripts/address.py add <recogniseId>`

## 业务约束

用户有购买意向时，知识在前、商品在后。全链路含交易流程均需结合知识输出。

商品搜索规则：

- 仅在用户表达购买意向时搜索商品，如“想买”“帮我找”“有没有卖的”。
- 纯知识咨询，如“xx 怎么样”“xx 和 yy 哪个好”，不触发搜索。
- 搜索方式：调用百度优选搜索。
- 准入规则：按相关性筛选，仅纳入与用户查询相关的商品。
- 结果不足 10 条时用同义词补充搜索，最多 3 次，含首次。
- 同义词必须保留用户指定的核心限定词，如“手机 typec 充电器”可改为“手机 USB-C 充电器”，不能丢“手机”。
- 用户通过对比或榜单做完决策后，主动询问是否需要搜索购买。

下单前必须确认地址：调用 `address list` 让用户明确选择，禁止默认下单。

地址添加两步依赖：必须先 `address recognise` 获取 `recogniseId`，再 `address add`，不可跳步。

## 展示规范

商品列表必须用表格：

| 序号 | 商品名称 | 价格 | 商城 | 店铺 | 其他 |
| --- | --- | --- | --- | --- | --- |
| 1 | 商品名称 | ¥xx起 | 百度优选 | 店铺名 4.9分 | 销量170 / 7天无理由 / 3种规格 |

商品名称必须展示接口返回的完整商品名，禁止截断或简化。

价格：多 SKU 显示 `¥xx起`，单 SKU 显示 `¥xx`。

商城：百度优选。

店铺：有评分时显示 `店铺名 x.x分`，无评分只显示名称。

其他：销量大于 0、保障标签、规格数大于 1 时展示，用 `/` 分隔。

品牌榜单列表中，品牌名称使用 `brandLandingURL` 作为品牌名跳转链接。

## 下单流程

百度优选商品严格按顺序执行：

1. 商品选择：调用 `spu list` 搜索，展示结果，用户选择，获取 `spuId`。
2. 规格选择：从搜索结果或 `spu detail` 获取 SKU 列表。
3. 仅 1 个 SKU：自动使用，跳过确认。
4. 多个 SKU：展示规格让用户选择，获取 `skuId`。
5. 无匹配 SKU：禁止使用不匹配的 SKU 下单，告知用户当前商品无匹配规格，引导重新搜索或选择其他商品。
6. 地址确认：调用 `address list` 获取地址列表。
7. 有地址：展示列表让用户选择，同时提示可新增地址，获取 `addrId`。
8. 无地址：引导用户提供地址信息，格式为收货人、详细地址、手机号；调用 `address recognise` 后再调用 `address add`，获取 `addrId`。
9. 订单确认：汇总展示商品名称、规格、收货地址、金额，等待用户确认。
10. 创建订单：调用 `order create`，返回订单详情链接。

创建订单使用的账号为用户申请 token 的账号。订单创建后用户需在返回的链接中完成支付。

## 错误处理

- `token is limit`：静默等待 1 秒后重试同一请求，不可跳过或用其他结果替代。
- `token权限不足`：告知用户访问 https://openai.baidu.com 申请。
- `token is nil` 或 `token is invalid`：提示用户检查 `BAIDU_EC_SEARCH_TOKEN` 配置。
- `path错误`、`请求地址错误`、`非法path`：检查脚本路径和参数。
- `商品已下架`、`商品已售罄`：引导选择其他商品或规格。
- `不支持用户地址发货`：引导修改收货地址。

不要向用户展示原始 `errmsg`，需要转译为用户友好的提示。
