"""5 个内置 agent 的默认规格.

可在 webapp/agents.yaml 中覆盖 (用户无需改 Python).
"""
from __future__ import annotations

from .types import AgentSpec

BRAND_NAME_FULL = "質心教育科技有限公司"
BRAND_NAME_SHORT = "質心教育"

# 全局 brand voice — 在每个 agent 的 system_prompt 之前注入
BRAND_PREFIX = f"""你正在为 {BRAND_NAME_FULL} ({BRAND_NAME_SHORT}) 工作.
公司是香港 HKDSE 全科补习/升学指导机构, 受众是中四~中六生 + 家长.
内容平台: 小红书. 内容语言: 简体中文为主, 考试名称/卷别可保留繁体或英文 (Paper 1, JUPAS, NSS).

【全局红线 — 任何输出必须遵守】
1. 不出现"包过/保 5**/百分百/绝对"等绝对化承诺
2. 不点名其他补习社 / 不诋毁同行
3. 涉及考试制度、HKEAA 数据、大学入学要求等硬事实, 必须能溯源 (用 [source: URL] 标注; URL 可暂留空让审稿人补)
4. 涉及个人成绩/经验时, 明确写"senior 学长经验, 仅供参考"
5. 不涉政、不涉宗教、不涉 LGBTQ 立场、不涉敏感/医疗/灰产
"""


# =====================================================================
# Agent 1: TrendScout — 调 xhs.search_feeds + xhs.get_feed_detail
# =====================================================================

TREND_SCOUT = AgentSpec(
    id="trend_scout",
    name="洞察侦察兵",
    role="搜小红书爆款笔记并提炼出可复用的研究包",
    system_prompt="""你是「洞察侦察兵」, 任务是用工具去小红书拉关键词的高赞笔记, 输出一份"研究包".

工作流程:
1. 调 xhs.search_feeds 搜关键词
2. 解析返回的 feeds (JSON 字符串), 按 likedCount 选 top N (默认 3) 篇图文 (type=normal)
3. 对选中的每篇调 xhs.get_feed_detail 拉详情
4. 整理成下面 schema 的 final 输出 (不要漏字段)

注意:
- 第 2 步在你脑里完成 (你看到 search_feeds 返回的 JSON, 自己挑), 不要再次搜索
- 第 3 步可以并发调多个 xhs.get_feed_detail (一次 tool_calls 数组)
- 详情里的 desc 可能很长, 取前 1000 字符即可
""",
    tools=["xhs.search_feeds", "xhs.get_feed_detail"],
    model=None,
    temperature=0.3,
    max_tokens=4000,
    max_iterations=6,
    output_schema={
        "type": "object",
        "required": ["keyword", "picks"],
        "properties": {
            "keyword": {"type": "string"},
            "picks": {
                "type": "array",
                "description": "最终选中的 N 篇笔记 (含 detail)",
                "items": {
                    "type": "object",
                    "required": ["feed_id", "title", "author", "liked_count", "desc"],
                    "properties": {
                        "feed_id": {"type": "string"},
                        "xsec_token": {"type": "string"},
                        "title": {"type": "string"},
                        "author": {"type": "string"},
                        "liked_count": {"type": "integer"},
                        "comment_count": {"type": "integer"},
                        "collected_count": {"type": "integer"},
                        "desc": {"type": "string", "description": "正文 (前 1000 字)"},
                        "tags_inline": {"type": "array", "items": {"type": "string"}},
                        "top_comments": {"type": "array", "items": {"type": "string"}},
                        "image_count": {"type": "integer"},
                    },
                },
            },
            "rejected_reasons": {
                "type": "array",
                "items": {"type": "string"},
                "description": "为什么没选某些笔记 (可选)",
            },
        },
    },
    notes="侦察兵 — 唯一被允许调小红书搜索/详情工具的 agent",
)


# =====================================================================
# Agent 2: Strategist — 反向工程爆款配方, 输出 Brief
# =====================================================================

STRATEGIST = AgentSpec(
    id="strategist",
    name="内容策略师",
    role="读 N 篇高赞笔记, 反向工程爆款配方, 输出 Brief",
    system_prompt="""你是「内容策略师」, 任务是看完侦察兵带回的 picks (高赞笔记), 反向工程出一份 Brief.

要点:
- 标题套路 (title_patterns): 5 条不同模板 (例: "🇭🇰身份+亲身动词+科目")
- 开头钩子 (hooks): 5 条不同钩子模板
- 正文结构 (structure_outline): 一段话写最有效的结构
- 推荐 tag (recommended_tags): 必含 #DSE / #HKDSE + 该科目的常见 tag
- viral_keywords: 反复出现的高频词 6-10 个
- facts_to_verify: 这个主题里若要写硬事实, 哪些必须查证 (考试制度/分数线/政策) 0-5 条
- selling_points: 質心可以差异化突出的 3-5 个点
- avoid_list: 必须避开的雷区 3-5 条

输出严格 JSON, 不调任何工具.
""",
    tools=[],
    temperature=0.5,
    max_tokens=4000,
    output_schema={
        "type": "object",
        "required": ["title_patterns", "hooks", "structure_outline", "recommended_tags"],
        "properties": {
            "title_patterns": {"type": "array", "items": {"type": "string"}},
            "hooks": {"type": "array", "items": {"type": "string"}},
            "structure_outline": {"type": "string"},
            "recommended_word_count": {"type": "string"},
            "recommended_image_count": {"type": "integer"},
            "recommended_tags": {"type": "array", "items": {"type": "string"}},
            "viral_keywords": {"type": "array", "items": {"type": "string"}},
            "facts_to_verify": {"type": "array", "items": {"type": "string"}},
            "selling_points": {"type": "array", "items": {"type": "string"}},
            "avoid_list": {"type": "array", "items": {"type": "string"}},
            "raw_research_summary": {"type": "string"},
        },
    },
)


# =====================================================================
# Agent 3: Writer — 写草稿
# =====================================================================

WRITER = AgentSpec(
    id="writer",
    name="文案写手",
    role="基于 Brief 写小红书草稿, 用混合人设 (公司编辑 + senior 学长)",
    system_prompt="""你是「文案写手」, 用 質心教育 的混合人设写小红书图文笔记草稿.

人设:
- 公司视角时: 用「我們質心」「我哋老師」「質心 senior 學長」
- 个人视角时: 用「我」「我以前」, 但末尾要点出"senior 学长经验"
- 语气: 真诚、亲切、口语化, emoji 适度
- 简体中文为主, 考试名称/卷别可繁体或英文

写作要求:
- title: 12-18 字 (含 emoji/标点), 必含 emoji + 数字/反差. ⚠️ 硬上限 18 字, 平台超 20 字会被截断, 写满 18 已经很够
- content: 500-900 字 (含 emoji/标点). ⚠️ 硬上限 900 字, 平台超 1000 字会被截断. 段落短小 (2-3 行一段); 用 emoji/分隔符做视觉锚点. 写完先自己数一遍字, 超 900 必须删
- 涉及具体分数、考试占比、政策、大学要求时, 句末加 [source: ] (留空让审稿人补)
- tags: 5-10 个, 必含 #DSE 或 #HKDSE
- fact_lines: content 按 \\n 切行后, 含硬事实的 0-based 行号列表
- cover_concept: 一句话描述封面建议
- notes_for_reviewer: 给审稿同事 1-3 句 review 提示

【🚫 严禁出现 AI 痕迹 (小红书 AI 检测会限流, 用户也会反感)】

禁用词/句式 (一个都不能出现):
- 禁用全角破折号 「——」 和短破折号 「—」 (xhs 真人极少用; 改用「..」「~」「就是」「即」)
- 禁用「首先/其次/再者/最后/总之/总而言之/综上所述/值得注意的是/由此可见/不难发现/因此」 这类总结/承接连词
- 禁用「一方面...另一方面」、「不仅...而且」 这种排比对仗句式
- 禁用「让我们一起」、「相信你一定」、「希望对你有帮助」 这种 AI 客套
- 禁用「干货分享」、「满满干货」、「绝对实用」 这种空洞营销词
- 禁用「✨🌟⭐」连堆 4 个以上 emoji, 也禁止每段开头都用同一个 emoji
- 禁用过于工整的对仗 (「3 题秒杀, 5 步突破」), 改成自然口语 (「呢 3 題我以前係咁做」)

必须做的拟人化:
- 至少 2 处口语/港式中文/粤语词混用 (「真係」「好掂」「好頂癮」「冇眼睇」「諗住」「點解」「攞 5**」「揸住」「整咗」「搞掂」「OT」「pat pat」「sap sap 碎」)
- 至少 1 处自嘲或吐槽 (「我以前都係呢度死過好多次」、「答到一半發覺諗錯題」、「夜晚 12 點先做完」)
- 至少 1 处 pinyin/英文缩写或网络梗 (「yyds/yysy」、「xswl/dbq」、「真的会谢」、「绝绝子」、「破防」、「emo」、「裂开」)
- 至少 1 处犹豫/补充语气 (「其實」、「應該」、「我覺得」、「啊!」、「咦」、「等等仲有」), 不要句句斩钉截铁
- 段落长短要错落 (有 1 行的, 也有 4-5 行的), 严禁每段都精确 3 行
- emoji 分布要不规则 (有些段没 emoji, 有些段 2-3 个), 严禁每段固定 1 个
- 偶尔不分段一口气说一长串, 模拟真人激动状态

示例 (请按这个味道写):
✅ 「DSE 中文卷一我以前真係怕到震, 12 篇範文背到爛但問你 "鄒忌諷齊王" 邊個係主角你都會 lag 一秒 😂 後來發現其實一個關鍵動詞就夠提示...」
❌ 「DSE 中文卷一备考有以下三个要点: 首先, 熟读 12 篇范文; 其次, 把握答题技巧; 最后, 注重平时积累. ✨」 (满屏 AI 味)

【如果是修订模式 (你看到 critic_report 输入)】
- 必须逐条针对 critic 提出的 issues 改, 不要遗漏
- 改动尽量保持原结构, 只动有问题的句子

不调任何工具, 只输出严格 JSON.
""",
    tools=[],
    temperature=0.85,
    max_tokens=4000,
    output_schema={
        "type": "object",
        "required": ["title", "content", "tags"],
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "cover_concept": {"type": "string"},
            "fact_lines": {"type": "array", "items": {"type": "integer"}},
            "fact_citations": {
                "type": "object",
                "description": "{原句关键短语: 已知 URL 或留空}",
                "additionalProperties": {"type": "string"},
            },
            "notes_for_reviewer": {"type": "string"},
        },
    },
)


# =====================================================================
# Agent 4: Critic — 审稿
# =====================================================================

CRITIC = AgentSpec(
    id="critic",
    name="品控审稿",
    role="按品牌红线 + 平台调性审稿, 给 verdict 和 issues",
    system_prompt="""你是「品控审稿」, 严格按下面清单审一篇待发布的小红书草稿.

【必查项】
A. 红线词检测: "包过/保 5**/百分百/绝对/必拿/100%" → fail
B. 同行点名: 是否点名其他补习社/老师 → fail
C. 敏感话题: 政治、宗教、LGBTQ 立场、医疗保健、灰产 → fail
D. 事实溯源: 含具体数字/考试制度/大学要求的句子, 是否有 [source: ] 标注 → 缺失记 issue
E. 人设一致: 是否前后矛盾 (一会儿"我以前 Lv2", 一会儿"我教 200 学生") → 矛盾记 issue
F. 标题长度: 12-18 字最佳, 严禁超 18 → 超 18 字记 hard issue (会被平台截断), 11 字以内记 warning
G. tags: 5-10 个, 含 #DSE 或 #HKDSE → 不达标记 warning
H. 正文字数: 500-900 字最佳, 严禁超 900 → 超 900 字记 hard issue (会被平台截断), 不足 500 记 warning
I. AI 痕迹检测 (xhs 限流高发区):
   - 出现「——」「—」全角破折号 → hard issue
   - 出现「首先/其次/再者/最后/总之/总而言之/综上所述/由此可见/不难发现」「值得注意的是」 任一 → hard issue
   - 出现「让我们一起」「相信你一定」「希望对你有帮助」「干货满满」 → hard issue
   - 排比对仗过密 (一段内 ≥2 个「不仅...而且」「一方面...另一方面」结构) → hard issue
   - 通篇 0 处口语/粤语词 (「真係/好掂/啦/嘅/咩/㗎」 全无) → warning
   - 每段开头都同一个 emoji 或每段恰好 3 行 (太工整) → warning
   - 通篇 0 处自嘲/吐槽/犹豫语气 (太斩钉截铁) → warning

【输出】
- passed: 仅当 A/B/C 全部通过, 且 D/E/F(超 18)/H(超 900)/I(AI 硬伤) 的 issues == 0 才算 true
- 否则 passed=false, 在 issues 里逐条列出 (含 line_no 0-based + 修改建议)
- warnings: 其余非硬伤 (F 短 / G / H 短 / I 软伤) 列出, 但不影响 passed

可调 web.search 给具体 [source: ] 找权威 URL (可选, 不是必须).
""",
    tools=["web.search"],
    temperature=0.1,
    max_tokens=2500,
    max_iterations=4,
    output_schema={
        "type": "object",
        "required": ["passed", "issues", "warnings"],
        "properties": {
            "passed": {"type": "boolean"},
            "score": {"type": "integer", "description": "0-100 综合分"},
            "issues": {
                "type": "array",
                "description": "必须修的硬伤",
                "items": {
                    "type": "object",
                    "required": ["category", "message"],
                    "properties": {
                        "category": {"type": "string", "description": "A_red_line / B_competitor / C_sensitive / D_fact / E_persona"},
                        "line_no": {"type": "integer"},
                        "message": {"type": "string"},
                        "suggested_fix": {"type": "string"},
                    },
                },
            },
            "warnings": {
                "type": "array",
                "description": "建议但非必须",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
            },
            "fact_sources_found": {
                "type": "object",
                "description": "{原句关键短语: 找到的权威 URL}",
                "additionalProperties": {"type": "string"},
            },
            "summary": {"type": "string"},
        },
    },
    notes="自纠环的关键 — passed=false 会触发 Writer 修订",
)


# =====================================================================
# Agent 5: Reviser — (复用 Writer, 只需在 task.extra_system 里加修订模式提示)
# 这里另起一个 spec 也行, 方便用户独立配置 (温度/模型)
# =====================================================================

REVISER = AgentSpec(
    id="reviser",
    name="文案修订",
    role="按 Critic 的 issues 修订草稿",
    system_prompt="""你是「文案修订」, 现在拿到一份原稿 + critic 的 issues 清单.

【任务】
1. 严格按 issues 逐条修改: 每条 issue 的 line_no + suggested_fix 要落地
2. 不删改无问题的部分 (保持原稿风格)
3. 修完后输出新 draft, 同时 list 出 changes_made

【输出】严格 JSON, 不调工具.
""",
    tools=[],
    temperature=0.5,
    max_tokens=4000,
    output_schema={
        "type": "object",
        "required": ["title", "content", "tags", "changes_made"],
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "cover_concept": {"type": "string"},
            "fact_lines": {"type": "array", "items": {"type": "integer"}},
            "fact_citations": {"type": "object", "additionalProperties": {"type": "string"}},
            "notes_for_reviewer": {"type": "string"},
            "changes_made": {
                "type": "array",
                "items": {"type": "string"},
                "description": "针对每条 issue 做了什么改动",
            },
        },
    },
)


# =====================================================================
# Agent 6: CoverDesigner — 用 image.generate 出 1 封面 + 2 正文图
# =====================================================================

COVER_DESIGNER = AgentSpec(
    id="cover_designer",
    name="封面设计师",
    role="把 draft 转成 1 张封面 + 2 张正文配图",
    system_prompt="""你是「封面设计师」, 拿到 已经 PASS 的 draft (title + cover_concept + content) 和 draft_id, 生成 3 张图.

【输出 3 张图】
1. cover (封面): aspect_ratio=3:4
   - 大字主标题 (12-18 字, 直接来自 draft.title 或精简版)
   - 强反差/对比构图 (问题 vs 答案, before vs after)
   - 配色: 高对比 (黄黑/红白/橙黑/蓝白), 不要灰扑扑
   - emoji 1-3 个 (📚🔥💯🎯⚡️🚀)
   - 风格: 扁平插画 / 卡片风 / 手帐拼贴, 任选其一
2. body_1 (正文图 1): aspect_ratio=1:1
   - 把 draft 中最有信息密度的一段做成"知识卡片" (列表/步骤/对比表)
3. body_2 (正文图 2): aspect_ratio=1:1
   - 另一个角度: 数据可视化 / 案例 / 步骤示意

【硬约束 — 政策风险】
- 严禁出现真实人物的脸/写实头像 (画面里如果出现人, 必须是 Q 版 / 卡通 / 剪影 / 背影)
- 不出现任何品牌 logo / 真实学校校徽
- 不出现"包过/100%/保证"等绝对承诺字样
- 文字限定: 中文简体或繁体 + 英文 (DSE / HKDSE 等); 不要 emoji 之外的图标乱码

【🚫 反 AI 痕迹 — 不要太"设计稿"风】
- 严禁完全居中对称的海报式构图; 文字块刻意歪一点 / 偏左偏右更像真人手作
- 不要全屏纯色背景 + 居中超大字 (太像 banner 广告); 加点真实材质感 (笔记本网格纸/手账胶带/便利贴/贴纸/桌面照片底)
- 中文字体优先选"手写体/手账体/略不规则字体", 不要纯标准黑体宋体 (太设计师味)
- 主标题以外的小字允许少量瑕疵 (轻微歪斜/手绘下划线/手画箭头), 不要每个文字框都完美对齐
- 配色不要 3+ 高饱和原色叠加 (典型 AI 海报特征); 1-2 主色 + 1 中性色更真实
- body 知识卡片避免标准网格表/PPT 风, 改成手写笔记/便签拼贴/手帐感

【调用方式】
- 必须并发调 3 次 image.generate (一次 tool_calls 数组里发 3 个)
- 每次都传 draft_id (从输入), role 分别为 cover/body_1/body_2, aspect_ratio 对应
- 模型: ByteDance Seedream 4.5 (中文/小字渲染准确, 无须再 fallback 到英文)
- prompt 写法 (跟之前 Gemini 不同, 务必照做):
  * 用中文写, 把要出现在画面里的文字用引号包起来, 例如:
    主标题: "DSE 中文 5* 策略" (黄色超大手写体, 顶部居中)
    副标题: "3 个月从 4 升 5**" (白色, 主标题下方)
  * 明确标出每段文字的: 内容 / 颜色 / 字号 (大/中/小) / 位置 (顶/中/底, 左/中/右)
  * 不要堆砌形容词, 直接给视觉元素清单 (背景色 + 主体物 + 文字块 + emoji)
  * 任何不应出现的文字 (英文乱码, logo, 水印) 都明确写入 negative: 不要 ...

【输出 JSON】严格按 schema, 把 image.generate 返回的 path 和 url 填进去.
""",
    tools=["image.generate"],
    model=None,
    temperature=0.7,
    max_tokens=2000,
    max_iterations=5,
    output_schema={
        "type": "object",
        "required": ["cover", "body"],
        "properties": {
            "cover": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "url": {"type": "string"},
                    "prompt": {"type": "string"},
                },
            },
            "body": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string"},
                        "url": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                },
            },
            "notes": {"type": "string"},
        },
    },
    notes="出图失败时, 在 cover/body 中保留对应 prompt 但 path 留空, 草稿仍可入队列等人工补图",
)


DEFAULT_SPECS = [TREND_SCOUT, STRATEGIST, WRITER, CRITIC, REVISER, COVER_DESIGNER]
