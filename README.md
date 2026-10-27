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
- **进度趋势图**：实际完成度和时间进度对比，鼠标移上去看每天的数值
- **本周进展**：本周每个 KR 变化了多少（如“跑步 10 → 14 次（+4）”）
- **预测**：数值型 KR 按最近 4 周的速度，显示预计完成日期，或者季末能到多少、每周还需要多少
- 点 KR 标题查看它的完整历史记录

每次修改都会直接写回 `okr/` 下的 Markdown 文件，并自动更新 [dashboard.md](dashboard.md)。

思路参考了 okr2go（一个 Markdown 文件就是全部数据，见 [GitHub okr 话题](https://github.com/topics/okr)）和 [AnandChowdhary/okrs](https://github.com/AnandChowdhary/okrs)（把 OKR 放在 git 仓库里跟踪）。

## 目录结构

```
My_OKR/
├── dashboard.md              # 自动生成的仪表盘（不要手动改）
├── data/okr.db               # 历史数据库（SQLite，自动生成）
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
│   ├── okr_db.py             # 历史数据库：同步、趋势、预测
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

## 历史数据库

`data/okr.db` 是一个 SQLite 数据库（Python 自带，不用安装），专门记录每个 KR 的数值**什么时候变成了多少**。

- **Markdown 仍然是主数据**。数据库只是在旁边记账：数据库删了，OKR 本身一点不受影响，下次打开界面会从当前数值重新开始记录。
- **只在数值变化时记一条**。在界面上 10 分钟内连续点 `＋`，合并成一条记录；改回原值的话，那条记录直接删掉。
- **Obsidian 里改的也会记下来**。下次打开界面或运行命令时，程序会对比上次的记录，时间取文件的修改时间。
- **KR 的身份**：每个 KR 行尾会自动加上一个块 ID（如 `^kr-3fa9c1`），这是 Obsidian 原生的写法，阅读模式下不显示。
  有了它，KR 改标题、挪位置都不会断开历史。
  请不要手动修改这个 ID；复制粘贴出了重复 ID，或者从上季度复制 KR 过来，程序都会自动换成新 ID。
- **备份与同步**：数据库会随 `git add -A` 一起提交。它是二进制文件，git 没法合并，所以尽量只在一台电脑上更新；
  换电脑之前先 `git pull`。

命令行：

```bash
okr sync                    # 手动同步（打开界面、生成周报时会自动同步）
okr history                 # 列出所有 KR 和记录数
okr history 跑步             # 某个 KR 的完整历史（按标题关键词或 ID）
okr sql "SELECT title, progress, updated_at FROM kr_latest ORDER BY progress"
```

`okr sql` 以**只读**方式打开数据库，写入语句会被拒绝，可以放心查询。表结构：

| 表 / 视图 | 内容 |
|---|---|
| `kr` | 每个 KR 一行：`id`、`quarter`、`objective`、`domain`、`title`、`position`、`first_seen`、`removed_at`（从文件中删除的时间） |
| `measurement` | 每次变化一行：`kr_id`、`ts`（本地时间）、`current`、`target`、`checked`、`dropped`、`progress`（0~1）、`source`（`gui` 界面 / `file` 文件） |
| `kr_latest` | 视图：每个 KR 加上它的最新数值 |

举例：本季度每个领域的平均进度

```bash
okr sql "SELECT domain, ROUND(AVG(progress)*100) AS pct FROM kr_latest WHERE quarter='2026-Q4' AND dropped=0 AND removed_at IS NULL GROUP BY domain"
```

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
| 每周（如周五） | 更新 KR 数值，点“本周周报”写总结（周报会自动列出本周每个 KR 的变化） |
| 季度末 | 点“季度复盘”，会自动带上最终得分和整个季度的变化 |

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
