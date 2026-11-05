# 接口与获取边界

维护脚本或排查获取失败时参考。以下说明对应当前脚本使用的接口和参数。

## PubMed 题录

基础地址：`https://eutils.ncbi.nlm.nih.gov/entrez/eutils/`

| 接口 | 脚本用途 | 批量参数 |
|---|---|---|
| `esearch.fcgi` | 检索 PMID，按相关性排序 | 每页 200 条 |
| `esummary.fcgi` | 获取题名、期刊、日期和 DOI | 每批最多 100 条 |

批次间隔为 0.35 秒。每个主题最多取回 `--max` 条候选；多主题合并后按 PMID、DOI 去重。题录用于寻找和核对全文，不代表已成功获取全文。下载流程不再批量获取摘要作为后备产物。

## PMC 官方原版 PDF

使用 [PMC 官方 AWS 数据集](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/) 的匿名 HTTPS 接口；对象布局与元数据字段见官方 [README](https://pmc-oa-opendata.s3.amazonaws.com/README.txt)。2026-10-24 已核对。无需 AWS 账号或认证。

1. 已知 PMCID 后，以 `https://pmc-oa-opendata.s3.amazonaws.com/?list-type=2&prefix={PMCID}.&delimiter=/` 枚举版本；处理默认 XML 命名空间及分页，不假定 `.1` 存在。
2. 逐版本获取 `metadata/{PMCID}.{版本}.json`，严格核对 PMID、PMCID 和已知 DOI。允许官方开放版本或作者手稿；优先非手稿版本，不能仅按版本号认定优先级。
3. 读取元数据的 `pdf_url`，只接受 `s3://pmc-oa-opendata/` 且属于当前文章版本的 PDF 路径，将其转换为同一官方桶的 HTTPS 地址。核对元数据给出的 MD5，再执行 PDF 身份与正文检查。

不使用未获官方文档支持的 Europe PMC `/fullTextPDF` 拼接地址，也不回退已停用的旧 OA/FTP 分发接口。

## Europe PMC 身份映射与 XML 全文

基础地址：`https://www.ebi.ac.uk/europepmc/webservices/rest/`

先用 `search?query=EXT_ID:{PMID} AND SRC:MED&format=json&resultType=core` 查询记录（参数须 URL 编码），核对返回的 PMID、`source=MED` 和已知 DOI，再获取 PMCID：

1. 有合法 PMCID 时，先按上一节尝试官方原版 PDF。
2. 未取得原版 PDF 时，请求 `{PMCID}/fullTextXML`，核对 JATS 身份、正文及渲染支持范围后转换为 PDF。
3. 未取得或未通过验证的全文：排除该候选，不生成摘要文件或失败文献清单。

PMCID 和 Europe PMC 状态字段不保证请求成功，不仅凭 `isOpenAccess`、`hasPDF` 或 `inEPMC` 排除获取机会。接口说明见 [Europe PMC REST API](https://europepmc.org/RestfulWebService)。

## 文件验证与结果解释

- PDF：必须使用 `pypdf`，核对可提取的正文及文献身份；不能仅检查 `%PDF`、字节数、HTTP 状态或文件存在。当前保守规则要求不少于 800 个非空白字符、正文章节标题及参考文献标题；身份在首页核对，显式 PMID/DOI 冲突时拒收，不能用参考文献中的标识符证明身份。不设最低页数。缺少此依赖时在开始任务前报错，不能放宽校验。
- XML：必须有实质正文，摘要不能替代 `<body>`。先核对身份与渲染支持范围，再渲染并检查 PDF；当前转换器保守拒绝不能可靠保留的图、复杂表格、公式或其他不支持的内容，不能静默丢弃后仍标为全文。
- 自动校验仅用于过滤明显错误，不证明全文完整。交付前还应抽查正文和关键内容；发现缺失时从成功集合中移除，并同步重建清单和索引。
- 扫描件、缺少标准英文章节标题的文章或极短全文可能无法自动确认，因此不收录；这不等于没有全文。校验当前交付时从固定八列 CSV 读取题录，重新验证 PDF 的正文和身份，不依赖历史文件摘要值。下载时对官方元数据 MD5 的核对仍用于验证该次传输。
- MD 首行直接为 `# 文献清单`，只供阅读；校验核对其表格、计数和文件链接，不将整篇文本与模板逐字比较。旧首行机器注释可兼容跳过，其中 JSON 不作为数据来源，新清单不再生成该注释。
- 未完成任务只从外部临时检查点恢复，严格核对完整任务参数。已完成任务的 `--resume` 重新检查现存 MD、CSV 与 PDF 后直接返回，不重新检索，也不依赖已清除的检查点比较旧参数。补充检索从 CSV 恢复题录，原查询从 MD 可读检索记录读取。
- 未取得全文可能与接口覆盖、访问限制、网络或渲染失败有关，不能统一解释为订阅制或版权限制。仅记录必要的任务层面限制，不输出失败候选的题录、摘要、标识符或待补名单。
- 补充全文获取渠道时先核实接口与获取边界，不绕过验证码或付费墙。

文件结构、成功集合和历史记录的约束见 [固定输出规范](output-contract.md)。
