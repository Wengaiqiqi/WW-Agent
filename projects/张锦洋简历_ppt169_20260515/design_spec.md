# 张锦洋个人简历 - Design Spec

## I. Project Information

| Item | Value |
| ---- | ----- |
| **Project Name** | 张锦洋个人简历 |
| **Canvas Format** | PPT 16:9 (1280×720) |
| **Page Count** | 13 |
| **Design Style** | A) General Versatile + 简洁科技风 |
| **Target Audience** | 招聘方 / HR / 技术面试官 |
| **Use Case** | 求职面试、简历投递 |
| **Created Date** | 2026-05-15 |

---

## II. Canvas Specification

| Property | Value |
| -------- | ----- |
| **Format** | PPT 16:9 |
| **Dimensions** | 1280×720 |
| **viewBox** | `0 0 1280 720` |
| **Margins** | 左右 60px，上下 50px |
| **Content Area** | 1160×620（从 60,50 到 1220,670） |

---

## III. Visual Theme

### Theme Style

- **Style**: 简洁科技风
- **Theme**: Light theme
- **Tone**: 专业、现代、技术感、青年活力

### Color Scheme

| Role | HEX | Purpose |
| ---- | --- | ------- |
| **Background** | `#FFFFFF` | 页面主背景 |
| **Secondary bg** | `#F4F6F9` | 卡片背景、区块背景 |
| **Primary** | `#1565C0` | 标题装饰、关键区块、图标 |
| **Accent** | `#0D47A1` | 数据高亮、关键信息 |
| **Secondary accent** | `#42A5F5` | 次要强调、渐变过渡 |
| **Body text** | `#212121` | 正文文字 |
| **Secondary text** | `#616161` | 注释、副标题 |
| **Tertiary text** | `#9E9E9E` | 页脚、补充信息 |
| **Border/divider** | `#E0E0E0` | 卡片边框、分割线 |
| **Success** | `#2E7D32` | 奖项/成果标记 |
| **Warning** | `#E65100` | 无特定用途，预留 |

### Gradient Scheme

```xml
<linearGradient id="titleGradient" x1="0%" y1="0%" x2="100%" y2="100%">
  <stop offset="0%" stop-color="#1565C0"/>
  <stop offset="100%" stop-color="#42A5F5"/>
</linearGradient>
<radialGradient id="bgDecor" cx="85%" cy="15%" r="45%">
  <stop offset="0%" stop-color="#1565C0" stop-opacity="0.08"/>
  <stop offset="100%" stop-color="#1565C0" stop-opacity="0"/>
</radialGradient>
```

---

## IV. Typography System

### Font Plan

**Typography direction**: 统一现代 CJK 无衬线

| Role | Chinese | English | Fallback tail |
| ---- | ------- | ------- | ------------- |
| **Title** | `"Microsoft YaHei", "PingFang SC"` | `Arial` | `sans-serif` |
| **Body** | `"Microsoft YaHei", "PingFang SC"` | `Arial` | `sans-serif` |
| **Emphasis** | same as Body | same as Body | `sans-serif` |
| **Code** | — | `Consolas, "Courier New"` | `monospace` |

**Per-role font stacks**:

- Title: `"Microsoft YaHei", "PingFang SC", Arial, sans-serif`
- Body: `"Microsoft YaHei", "PingFang SC", Arial, sans-serif`
- Emphasis: same as Body
- Code: `Consolas, "Courier New", monospace`

> Concord 统一方案：全篇使用微软雅黑，标题与正文靠字号和粗细区分。

### Font Size Hierarchy

**Baseline**: Body = 20px（中等密度，简历内容点适中）

| Purpose | Ratio to body | px Range | Weight |
| ------- | ------------- | -------- | ------ |
| Cover title (hero headline) | 2.5-5x | 50-100px | Bold |
| Chapter / section opener | 2-2.5x | 40-50px | Bold |
| Page title | 1.5-2x | 30-40px | Bold |
| Subtitle | 1.2-1.5x | 24-30px | SemiBold |
| **Body content** | **1x** | **20px** | Regular |
| Annotation / caption | 0.7-0.85x | 14-17px | Regular |
| Page number / footnote | 0.5-0.65x | 10-13px | Regular |

---

## V. Layout Principles

### Page Structure

- **Header area**: 顶部 50-100px，含页面标题 + 蓝色下划线装饰
- **Content area**: 中间 500-600px，主信息展示区
- **Footer area**: 底部 30-40px，页码 + 姓名

### Layout Pattern Library

| Pattern | Suitable Scenarios |
| ------- | ----------------- |
| **Single column centered** | 封面、结束页 |
| **Symmetric split (5:5)** | 基本信息左右分栏 |
| **Asymmetric split (3:7)** | 标题+正文布局 |
| **Top-bottom split** | 项目经历详情（标题→内容） |
| **Two/three column cards** | 技能卡片、奖项列表 |
| **Full-bleed + floating text** | 封面、结束页 |
| **Negative-space-driven** | 自我评价（一句重点 + 留白） |

### Spacing Specification

**Universal**:

| Element | Recommended Range | Current Project |
| ------- | ---------------- | --------------- |
| Safe margin from canvas edge | 40-60px | 60px |
| Content block gap | 24-40px | 32px |
| Icon-text gap | 8-16px | 12px |

**Card-based layouts**:

| Element | Recommended Range | Current Project |
| ------- | ---------------- | --------------- |
| Card gap | 20-32px | 24px |
| Card padding | 20-32px | 24px |
| Card border radius | 8-16px | 12px |
| Single-row card height | 530-600px | 560px |
| Three-column card width | 360-380px each | 360px |

---

## VI. Icon Usage Specification

### Source

- **Built-in icon library**: `tabler-filled`
- **Usage method**: SVG placeholder `<use data-icon="tabler-filled/icon-name" .../>`

### Recommended Icon List

| Purpose | Icon Path | Page |
| ------- | --------- | ---- |
| 姓名/用户 | `tabler-filled/user` | P02 |
| 手机 | `tabler-filled/phone-call` | P02 |
| 邮箱 | `tabler-filled/mail` | P02 |
| 地址 | `tabler-filled/map-pin` | P02 |
| 教育/学校 | `tabler-filled/school` | P03 |
| 书本/课程 | `tabler-filled/book` | P03 |
| 实习/工作 | `tabler-filled/briefcase` | P04 |
| 机器人 | `tabler-filled/robot` | P05-P08 |
| 奖杯 | `tabler-filled/trophy` | P05-P08, P10-P11 |
| 编程 | `tabler-filled/code-circle` | P09 |
| 硬件/芯片 | `tabler-filled/cpu` | P09 |
| 工具/设置 | `tabler-filled/settings` | P09 |
| 数据库 | `tabler-filled/database` | P09 |
| 奖章 | `tabler-filled/award` | P10-P11 |
| 星标 | `tabler-filled/sparkles` | P01, P12, P13 |
| 对勾 | `tabler-filled/circle-check` | P10-P11 |
| 日历 | `tabler-filled/calendar` | P03 |
| 靶心/目标 | `tabler-filled/target` | P12 |
| 信息 | `tabler-filled/info-circle` | P02 |

---

## VII. Visualization Reference List

Catalog read: 71 templates

| Page | Template | Path | Summary-quote (verbatim) | Usage |
| ---- | -------- | ---- | ------------------------ | ----- |
| P09 | icon_grid | `templates/charts/icon_grid.svg` | "Pick for 4-9 parallel features/capabilities/services as icon cards — feature grid, service lineup, benefits matrix, brand values, product highlights. Skip for sequential ordering (use numbered_steps) or hierarchical layers (use pyramid_chart)." | 四大技能方向（硬件开发、编程语言、开发工具、数据处理）用图标卡片展示 |
| P10 | vertical_list | `templates/charts/vertical_list.svg` | "Pick for 3-6 numbered key points each with a short description — design principles, core tenets, action items, key takeaways, recommendations, executive summary points. Skip for icon-style cards (use icon_grid) or sequential steps (use numbered_steps)." | 奖项荣誉列表（上半部分 7-8 项） |
| P11 | vertical_list | `templates/charts/vertical_list.svg` | "Pick for 3-6 numbered key points each with a short description — design principles, core tenets, action items, key takeaways, recommendations, executive summary points. Skip for icon-style cards (use icon_grid) or sequential steps (use numbered_steps)." | 奖项荣誉列表（下半部分 7-8 项） |

**Runners-up considered**:

- `numbered_steps` | rejected for P10/P11: 奖项无先后顺序，不需要编号步骤感
- `labeled_card` | rejected for P09: 技能为平行特征而非多维度描述同一主体
- `basic_table` | rejected for P10/P11: 奖项荣誉不需要表格结构化数据

---

## VIII. Image Resource List

> 本次不使用图片资源（纯排版设计）。

---

## IX. Content Outline

### P01 - 封面（anchor）

- **Layout**: Full-bleed 渐变背景（主色→浅蓝）+ 居中文字
- **Title**: 张锦洋
- **Subtitle**: 个人简历
- **Info**: 人工智能专业 · 本科在读 · 18991321768
- **Decoration**: 右上角放射渐变装饰，sparkles 图标点缀

---

### P02 - 基本信息（dense）

- **Layout**: 左右非对称分栏（左 3 信息卡片 + 右 7 照片区/姓名大标题）
- **Title**: 基本信息
- **Content**:
  - 姓名：张锦洋
  - 年龄：22岁
  - 学历：本科
  - 政治面貌：群众
  - 电话：18991321768
  - 邮箱：3094084480@qq.com
  - 地址：南宁
- **Icons**: user, phone-call, mail, map-pin, info-circle

---

### P03 - 教育经历（breathing）

- **Layout**: 单列居中，时间线 + 学校卡片
- **Title**: 教育经历
- **Content**:
  - 学校：广西民族大学
  - 专业：人工智能 | 本科
  - 时间：2023.9 ~ 至今
  - 主修课程：人工智能、机器学习、深度学习、自然语言处理、计算机视觉、数据结构与算法、数据库原理、操作系统、计算机网络
- **Icons**: school, book, calendar
- **Visualization**: 课程用标签云/横排标签展示

---

### P04 - 实习经历（dense）

- **Layout**: 左侧时间轴 + 右侧内容卡片
- **Title**: 实习经历
- **Content**:
  - 公司：字节跳动
  - 岗位：数据标注师
  - 时间：2025.9 ~ 2025.10
  - 职责：参与字节跳动人工智能大模型（豆包）的监督微调（SFT）数据标注工作，负责标注处理前端交互相关数据，严格遵循标注规范，保证样本的准确性与一致性，确保模型训练数据的高质量。
- **Icons**: briefcase

---

### P05 - 项目经历：中国机器人及人工智能大赛（dense）

- **Layout**: Top-bottom split — 标题行 + 内容分栏（左：项目描述 + 职责 / 右：技术栈 + 成果）
- **Title**: 中国机器人及人工智能大赛备赛开发
- **Subtitle**: 技术负责人 | 2025.3 ~ 2025.8
- **Content**:
  - 项目描述：围绕开鸿机器人进行动作与代码逻辑设计开发
  - 职责：动作设计（倒地起身、奔跑等复杂动作）、整体代码逻辑设计、自动模式 + 遥控模式
  - 技术栈：机器人动作设计、舵机控制、代码逻辑开发、遥控系统适配
  - 成果：国家级二等奖
- **Icons**: robot, trophy

---

### P06 - 项目经历：睿抗机器人开发者大赛（dense）

- **Layout**: Top-bottom split
- **Title**: 睿抗机器人开发者大赛备赛开发
- **Subtitle**: 技术负责人 | 2025.3 ~ 2025.8
- **Content**:
  - 项目描述：针对百度智能车进行视觉识别与运行逻辑优化
  - 职责：PPLCNet 视觉识别模型训练与参数调优、智能车运行逻辑代码设计调试
  - 技术栈：PPLCNet 模型、视觉识别、模型调参、智能车运行逻辑开发
  - 成果：省级一等奖
- **Icons**: robot, trophy

---

### P07 - 项目经历：ROBOCOM 机器人开发者大赛（dense）

- **Layout**: Top-bottom split
- **Title**: ROBOCOM 机器人开发者大赛备赛开发
- **Subtitle**: 技术负责人 | 2025.3 ~ 2025.8
- **Content**:
  - 项目描述：六足机器人整体设计
  - 职责：行走/翻越/低身位爬行等核心功能、全程无线远程调试、通用 2.4G 遥控功能
  - 技术栈：足式机器人设计、无线调试开发、2.4G 遥控系统搭建、硬件功能适配
  - 成果：国家级三等奖
- **Icons**: robot, trophy

---

### P08 - 项目经历：AGV 小车硬件开发（dense）

- **Layout**: Top-bottom split
- **Title**: AGV 小车硬件开发与代码调试
- **Subtitle**: 技术负责人 | 2025.3 ~ 2025.8
- **Content**:
  - 项目描述：AGV 小车硬件设计 + ROS2 开发
  - 职责：硬件模块设计与选型、ROS2 小车控制代码开发、雷达建图与自动化巡航
  - 技术栈：AGV 小车硬件开发、ROS2、雷达建图、自动化巡航、代码调试
  - 成果：成功实现雷达建图与自动化巡航功能
- **Icons**: robot, trophy

---

### P09 - 技能概览（breathing）

- **Layout**: 2×2 图标卡片网格
- **Title**: 专业技能
- **Visualization**: icon_grid
- **Content**:
  - 硬件开发：熟悉 ESP32 系列，树莓派裸机开发和项目部署，UART/SPI/I²C 通信实战
  - 编程语言：Python、C++，良好数学与编程基础
  - 开发与工具：Linux、Docker 容器部署、Git 代码管理、舵机/电机代码控制
  - 数据处理：AI 模型数据标注、模型训练经验
- **Icons**: cpu, code-circle, settings, database

---

### P10 - 奖项荣誉（上）（dense）

- **Layout**: 单列 vertical list，每个条目一行
- **Title**: 奖项荣誉
- **Visualization**: vertical_list
- **Content**:
  - 2024 第7届"泰迪杯"数据分析技能赛本科及以上组三等奖
  - 2024 睿抗机器人开发者大赛国家二等奖
  - 2024 第十四届 APMCM 亚太地区大学生数学建模竞赛参与奖
  - 2024 蓝桥杯省级三等奖
  - 第6届广西大学生人工智能设计大赛三等奖
  - 第26届中国机器人及人工智能大赛国家三等奖
  - 2025 蓝桥杯省级二等奖
  - 2025 GPLT 程序设计天梯赛团队三等奖
- **Icons**: award, circle-check

---

### P11 - 奖项荣誉（下）（dense）

- **Layout**: 单列 vertical list，续上页
- **Title**: 奖项荣誉（续）
- **Visualization**: vertical_list
- **Content**:
  - 2025 睿抗机器人开发者大赛省级三等奖
  - 第27届中国机器人及人工智能大赛国家优秀奖
  - Robocom 马术机器人越野赛国家三等奖
  - Robocom 马术机器人障碍赛国家三等奖
  - Robocom 马术机器人竞速赛国家三等奖
  - 2025 第7届国际青年人工智能大赛总决赛国家三等奖
  - 2025 中国国际大学生创新大赛"建行杯"广西赛区选拔赛银奖
- **Icons**: award, circle-check

---

### P12 - 自我评价（breathing）

- **Layout**: 单列居中，三段评价，每段配图标
- **Title**: 自我评价
- **Content**:
  - 学习能力强，对 AI/机器人开发充满热情，掌握 Python、C++ 等语言，熟悉 Linux/Docker/Git
  - AI 专业在读，具备扎实理论基础，硬件开发、模型训练、数据标注、机器人设计方向有丰富实操经验
  - 多次担任国家级/省级竞赛技术负责人，带领团队取得多项优异成绩，具备项目管理与团队协作经验
- **Icons**: sparkles, target, heart

---

### P13 - 结束页（anchor）

- **Layout**: Full-bleed 渐变背景 + 居中文字
- **Title**: 感谢关注
- **Subtitle**: 张锦洋 · 18991321768 · 3094084480@qq.com
- **Decoration**: 同封面风格
- **Icons**: sparkles

---

## X. Speaker Notes Requirements

- **File naming**: `notes/01_cover.md` ~ `notes/13_ending.md`，与 SVG 文件名对应
- **Notes style**: Conversational — 模拟面试中口述的自我介绍
- **Presentation purpose**: Inform — 向面试官全面展示技术背景和项目能力
- **内容结构**: 每页 2-3 句要点，突出简历中未展开的细节

---

## XI. Technical Constraints Reminder

### SVG Generation Must Follow:

1. viewBox: `0 0 1280 720`
2. Background uses `<rect>` elements
3. Text wrapping uses `<tspan>` (`<foreignObject>` FORBIDDEN)
4. Transparency uses `fill-opacity` / `stroke-opacity`; `rgba()` FORBIDDEN
5. FORBIDDEN: `mask`, `<style>`, `class`, `foreignObject`
6. FORBIDDEN: `textPath`, `animate*`, `script`
7. Text characters: write typography & symbols as raw Unicode; HTML named entities FORBIDDEN
8. `clipPath` only allowed on `<image>` elements
