# Agent Skills

面向科研文献工作的中文 Agent Skills：检索并获取可验证的开放全文，再对已有文献进行筛选、精读、局部翻译和跨文献比较。

## 技能目录

| Skill | 用途 | 主要产物 |
| --- | --- | --- |
| [literature-download](skills/literature-download/SKILL.md) | 按主题检索 PubMed，从 PMC 官方开放全文和 Europe PMC 获取、验证并去重全文 | `文献清单.md`、`文献索引.csv`、`论文/` |
| [literature-reading](skills/literature-reading/SKILL.md) | 阅读本地文献，筛选、精读、局部翻译并比较已读研究 | `项目说明.md`、`文献阅读.md` |

两个技能可以独立使用，也可以按“下载 → 阅读”的顺序配合使用。安装技能本身不会自动运行检索、下载或阅读任务。

## 安装

先克隆仓库，或从 GitHub 下载并解压仓库 ZIP：

```sh
git clone https://github.com/gao0329/agent-skills.git
cd agent-skills
```

将需要的技能**整个文件夹**复制到所用客户端的用户技能目录，不要只复制 `SKILL.md`：

| 客户端 | 用户技能目录 |
| --- | --- |
| Codex | `~/.agents/skills/` |
| WorkBuddy | `~/.workbuddy/skills/` |

例如，安装后应能找到 `~/.agents/skills/literature-download/SKILL.md`，下载技能的 `scripts/`、`references/` 和 `requirements.txt` 也应一并保留。Windows 下 `~` 指当前用户目录；其他客户端请使用各自支持的技能目录。已有同名技能时，请先备份并比较差异，再决定是否替换。

在 Codex 中，也可以让内置安装技能按 GitHub 路径安装：

```text
使用 $skill-installer，从 gao0329/agent-skills 安装
skills/literature-download 和 skills/literature-reading。
```

安装后，在新对话或客户端重新加载技能后使用。

## 运行依赖

### literature-download

- Python 3。本次发布在 Python 3.13 环境完成本地检查。
- 必需依赖：`pypdf`，用于检查 PDF 正文和文献身份。
- 需要联网访问 PubMed、Europe PMC 和 PMC 官方开放全文数据源。
- XML 全文转 PDF 需要 Chrome 或 Edge；当前脚本自动查找 Windows 常见 Chrome/Edge 安装路径和 Linux 常见 Chrome/Chromium 路径。其他路径和平台需调整脚本的 `find_chrome()`。缺少浏览器时仍可尝试获取原版 PDF。

在实际执行技能脚本的 Python 环境中安装依赖。从仓库根目录执行：

```sh
python -m pip install -r skills/literature-download/requirements.txt
```

也可以直接运行脚本查看参数：

```sh
python skills/literature-download/scripts/pipeline.py --help
```

### literature-reading

这是说明型技能，没有独立 Python 脚本。运行环境需要能读取本地 Markdown、CSV 和 PDF，并提取正文；扫描件需要环境额外提供 OCR。不能读取的材料应如实标注，不作为已经完成全文阅读的依据。

## 使用示例

### 1. 下载文献

在支持技能的客户端输入：

```text
使用 literature-download，查找高血压远程监测与自我监测相关文献。
每个主题最多检索 100 条候选，把实际取得并通过验证的全文保存到“本次文献”。
```

或从仓库根目录直接调用：

```sh
python skills/literature-download/scripts/pipeline.py --topic "hypertension telemonitoring" --topic "hypertension selfmonitoring" --scope "高血压远程监测与自我监测" --max 100 --out "本次文献"
```

`--max` 是每个主题的候选上限，不是保证下载篇数。脚本按关键词组合检索，不接受完整布尔检索式。输出结构固定为：

```text
本次文献/
├── 文献清单.md
├── 文献索引.csv
└── 论文/
    └── <已验证的全文 PDF>
```

原版 PDF 优先；符合当前转换器支持范围的 XML 正文可渲染为 PDF。只有摘要、获取失败或未通过验证的文献不计入全文成果。自动验证不能证明全文绝对完整，交付前仍需抽查正文和关键内容。

### 2. 阅读已有文献

```text
使用 literature-reading，阅读“本次文献”中的材料。
主题是高血压远程监测与自我监测。
先筛选全部题录，再简读 15 篇、重点阅读 5 篇，对其中 2 篇做相关段落的局部翻译。
项目根目录为“高血压研究”，请注明所有结论的原文依据。
```

阅读技能保留原文献目录，在项目根目录维护 `项目说明.md` 和 `文献阅读.md`。未指定数量时，默认目标为 25 篇候选简读、10 篇重点阅读、3 篇局部翻译，均可由用户调整；材料不足时保留真实数量。

## 维护与许可

每个技能独立保存在 `skills/<skill-name>/`，以 `SKILL.md` 为入口。更新时一起维护其引用的脚本和参考文件。不要提交真实密钥、运行记录、下载的论文或研究项目文件。

技能说明和代码使用 [MIT License](LICENSE)。通过技能获取的论文及第三方内容仍适用其各自的许可证，本仓库的 MIT 许可不延伸到这些内容。
