"""评测集：RAG 检索用例 + Text2SQL 用例（阶段五验收依据）。

【零基础说明：怎么给 RAG / Text2SQL "打分"？】
- RAG：对每个问题做检索，看召回的知识块里是否包含"预期关键词"。
  命中 = 检索系统找对了内容。这是检索评测最常用的轻量做法
  （生产环境一般再用人工标注或 LLM 打分，这里用关键词足够客观）。
- Text2SQL：把问题真跑一遍（生成 SQL -> 沙箱校验 -> 执行），
  判定：①执行成功 ②用到了预期的表 ③结果行数达标。
  用例分 simple（单表查询）与 complex（聚合/排序/JOIN/时间窗）两档分别算成功率。

expect_any 的语义：列表里任意一组关键词全部命中即算通过（同义问法容错）。
"""

# ============ RAG 检索评测用例 ============
RAG_CASES: list[dict] = [
    {"question": "色差公差一般是多少", "expect_any": [["ΔE", "公差"], ["1.5"]]},
    {"question": "ISO 12647-2 对 CMYK 的 Lab 目标值是多少", "expect_any": [["12647"], ["Lab"]]},
    {"question": "实地密度的参考范围是多少", "expect_any": [["密度", "1.40"], ["实地密度"]]},
    {"question": "印刷压力过大会有什么问题", "expect_any": [["网点增大"], ["糊版"]]},
    {"question": "什么是水墨平衡", "expect_any": [["润版液"], ["油水相斥"]]},
    {"question": "G7 是什么，核心是什么", "expect_any": [["G7"], ["灰平衡"]]},
    {"question": "印刷车间温度湿度应该控制在多少", "expect_any": [["20~26"], ["相对湿度"]]},
    {"question": "什么是网点增大 TVI", "expect_any": [["网点增大"], ["TVI"]]},
    {"question": "四色印刷的色序怎么安排", "expect_any": [["色序"], ["叠印"]]},
    {"question": "背面蹭脏怎么解决", "expect_any": [["蹭脏"], ["喷粉"]]},
    {"question": "系统判定 ΔE 超差的阈值是多少", "expect_any": [["1.5"], ["超差"]]},
    {"question": "ΔD 密度差是什么含义", "expect_any": [["密度差"], ["ΔD"]]},
]

# ============ Text2SQL 评测用例 ============
# must_tables_any：生成 SQL 里出现这些表中的任意一张即算用对了表
# min_rows：执行结果至少多少行（数据依赖回填+实时采集，均为真实库）
SQL_CASES: list[dict] = [
    {"question": "查询1号机组最近1小时的ΔE记录", "kind": "simple",
     "must_tables_any": [["color_measurements"]], "min_rows": 1},
    {"question": "查询设备列表", "kind": "simple",
     "must_tables_any": [["devices"]], "min_rows": 2},
    {"question": "查询1号机组最近1小时的压力实际值与初始压力", "kind": "simple",
     "must_tables_any": [["plc_readings"]], "min_rows": 1},
    {"question": "列出最近的告警记录", "kind": "simple",
     "must_tables_any": [["alarms"]], "min_rows": 0},
    {"question": "过去24小时每个机组的平均ΔE是多少，按从高到低排序", "kind": "complex",
     "must_tables_any": [["color_measurements"]], "min_rows": 5},
    {"question": "过去24小时哪些机组出现过ΔE超差，各有多少次", "kind": "complex",
     "must_tables_any": [["alarms", "color_measurements"]], "min_rows": 1},
    {"question": "过去24小时各机组的ΔE合格率是多少", "kind": "complex",
     "must_tables_any": [["color_measurements"]], "min_rows": 5},
    {"question": "过去1小时哪个机组的ΔE最大", "kind": "complex",
     "must_tables_any": [["color_measurements"]], "min_rows": 1},
    {"question": "统计最近24小时每小时的平均ΔE", "kind": "complex",
     "must_tables_any": [["color_measurements"]], "min_rows": 1},
    {"question": "查询1号机组最近30分钟的实际压力和色差ΔE", "kind": "complex",
     "must_tables_any": [["plc_readings", "color_measurements"]], "min_rows": 1},
]
