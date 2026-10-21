# My OKR · 个人 OKR 管理

在 Obsidian（或任何 Markdown 编辑器）里写 OKR，用一个 Python 脚本自动汇总进度，生成 [dashboard.md](dashboard.md) 仪表盘。
数据都是本地的 Markdown 文件，用 git 保存历史。脚本只用 Python 标准库，不需要安装任何依赖。

思路参考了 okr2go（一个 Markdown 文件就是全部数据，见 [GitHub okr 话题](https://github.com/topics/okr)）和 [AnandChowdhary/okrs](https://github.com/AnandChowdhary/okrs)（把 OKR 放在 git 仓库里跟踪）。

## 目录结构

```
My_OKR/
├── dashboard.md              # 自动生成的仪表盘（不要手动改）
├── okr/
│   └── 2026/
│       ├── 2026-年度目标.md   # 年度方向
│       ├── 2026-Q4.md         # 季度 OKR（脚本只汇总这类文件）
│       ├── 2026-Q4-复盘.md    # 季度复盘
│       └── weekly/
│           └── 2026-W43.md    # 每周简报
├── templates/                # 模板（脚本和 Obsidian 都能用）
├── scripts/okr_tool.py       # 汇总 / 生成工具
├── okr.bat                   # Windows 快捷入口
└── tests/
```

## KR 写法

季度文件里，`##` 二级标题是目标 O，后面的 `#标签` 是领域；下面的任务列表就是 KR：

```markdown
## O1 成为三菱机器人熟练工程师 #工作

- [ ] KR1 完成机器人项目 `2/3`          ← 数值型：67%
- [x] KR2 取得培训证书                   ← 勾选型：100%
- [ ] KR3 整理程序模板库 `8/20 个`       ← 可带单位：40%
- [ ] KR4 写一篇总结                     ← 未勾选且无数值：0%
- [-] KR5 本季度放弃的                   ← 不计入统计
```

- 目标进度 = 各 KR 平均值；季度进度 = 各目标平均值；超过目标按 100% 算
- 领域：`#工作` `#学习` `#健康`，取每个目标的第一个标签
- 没有任何 KR 的二级标题（如“备注”）会被忽略

## 状态判断

仪表盘会拿“完成度”和“季度时间已过去多少”做比较：

| 状态 | 条件 |
|---|---|
| 🟢 正常 | 完成度比时间进度落后不超过 10% |
| 🟡 预警 | 落后 10% ~ 25% |
| 🔴 落后 | 落后超过 25% |
| ✅ 完成 | 100% |

季度过半后，还在 0% 的 KR 会单独列在“还没动的 KR”里。

## 日常使用

Windows 下双击 `okr.bat` 会刷新仪表盘；也可以在命令行里带参数运行：

| 什么时候 | 命令 |
|---|---|
| 每年初 | `okr year` |
| 每季度初 | `okr quarter`，然后填写 O 和 KR |
| 每周（如周五） | 先在季度文件里更新 KR 数值，再 `okr week` 写周报 |
| 随时 | `okr dashboard`（或双击 `okr.bat`） |
| 季度末 | `okr review` 生成复盘，自动带上最终得分 |

非 Windows 用 `python scripts/okr_tool.py <命令>`。所有命令都可以加 `--date 2026-12-28` 指定日期；已存在的文件不会被覆盖。

改完记得提交：

```bash
git add -A && git commit -m "okr: 2026-W43 周更新" && git push
```

## Obsidian 设置建议

- 直接把本仓库文件夹作为 vault 打开（或放进现有 vault）
- 文件里的 `[[2026-Q4]]` 是双链，点击就能跳到对应文件
- 如果想在 Obsidian 里用模板：设置 → 核心插件 → 模板 → 模板文件夹位置选 `templates`
  （模板里的 `{{quarter}}` 等占位符需要手动替换；用脚本生成会自动填好）

## 测试

```bash
python -m unittest discover -s tests
```

## 旧内容

清空前的仓库内容保存在 tag `archive/pre-okr-reset` 中：

```bash
git checkout archive/pre-okr-reset -- <需要找回的路径>
```
