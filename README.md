# My OKR · 个人 OKR 管理

双击 `okr.bat` 打开网页界面，点按钮就能更新 KR 进度、添加目标、写周报；也可以直接在 Obsidian 里编辑。
数据都是本地的 Markdown 文件，用 git 保存历史。只需要装 Python（3.9 或更新），不需要安装其他依赖，也不用联网。

## 快速开始（Windows）

1. 安装 [Python](https://www.python.org/downloads/)，安装时勾选 **Add python.exe to PATH**
2. 双击仓库里的 `okr.bat`，浏览器会自动打开 OKR 界面
3. 用完关掉那个黑色命令窗口即可（它就是界面的后台服务）

界面里能做的事：

- KR 进度：数值型点 `－` / `＋` 或直接输入数字；勾选型点前面的方框
- 添加 / 编辑 / 删除目标和 KR，或把 KR 标记为“本季度放弃”
- 一键新建本周周报、季度复盘、年度目标、下季度 OKR，并跳到 Obsidian 里编辑
- 切换季度查看历史；从 Obsidian 改完切回浏览器会自动刷新

每次修改都会直接写回 `okr/` 下的 Markdown 文件，并自动更新 [dashboard.md](dashboard.md)。

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
├── scripts/
│   ├── okr_web.py            # 网页界面的后台服务
│   ├── web/index.html        # 网页界面
│   └── okr_tool.py           # 解析 / 汇总 / 生成文件（命令行也能用）
├── okr.bat                   # Windows 入口：双击打开界面
└── tests/
```

## KR 写法（在 Obsidian 里直接编辑时）

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
- 既没有 KR 也没有 `#标签` 的二级标题（如“备注”）会被忽略

## 状态判断

仪表盘会拿“完成度”和“季度时间已过去多少”做比较：

| 状态 | 条件 |
|---|---|
| 🟢 正常 | 完成度比时间进度落后不超过 10% |
| 🟡 预警 | 落后 10% ~ 25% |
| 🔴 落后 | 落后超过 25% |
| ✅ 完成 | 100% |

季度过半后，还在 0% 的 KR 会单独列在“还没动的 KR”里。

## 日常节奏

| 什么时候 | 做什么 |
|---|---|
| 每年初 | 界面里点“年度目标”，写下年度方向 |
| 每季度初 | 点“新建 20xx-Qx”，添加 2~4 个目标，每个目标 2~4 个 KR |
| 每周（如周五） | 更新 KR 数值，点“本周周报”写总结（周报会自动带上当前进度） |
| 季度末 | 点“季度复盘”，会自动带上最终得分 |

## 命令行（可选）

不想开界面时，也可以在命令行里带参数运行 `okr.bat`：

| 什么时候 | 命令 |
|---|---|
| 每年初 | `okr year` |
| 每季度初 | `okr quarter`，然后填写 O 和 KR |
| 每周（如周五） | 先在季度文件里更新 KR 数值，再 `okr week` 写周报 |
| 随时 | `okr dashboard` 刷新仪表盘 |
| 季度末 | `okr review` 生成复盘，自动带上最终得分 |

非 Windows 用 `python scripts/okr_tool.py <命令>`，网页界面用 `python scripts/okr_web.py`。所有命令都可以加 `--date 2026-12-28` 指定日期；已存在的文件不会被覆盖。

改完记得提交：

```bash
git add -A && git commit -m "okr: 2026-W43 周更新" && git push
```

## Obsidian 设置建议

界面里的“在 Obsidian 打开”按钮需要仓库文件夹已经作为 vault（或 vault 的一部分）在 Obsidian 里打开过。

- 直接把本仓库文件夹作为 vault 打开（或放进现有 vault）
- 文件里的 `[[2026-Q4]]` 是双链，点击就能跳到对应文件
- 如果想在 Obsidian 里用模板：设置 → 核心插件 → 模板 → 模板文件夹位置选 `templates`
  （模板里的 `{{quarter}}` 等占位符需要手动替换；用脚本生成会自动填好）

## 测试

```bash
python -m unittest discover -s tests
```

## 旧内容

清空前的仓库内容保存在 tag `v0.9.0`（提交 e9c6dd0）中：

```bash
git checkout v0.9.0 -- <需要找回的路径>
```
