"""Grounded request obligations shared by retrieval, prompting and compilation.

This is not a natural-language planner. Only explicit recognizable facts become
mandatory checks; open-ended preferences remain questions, never default values.
The model still proposes every executable task. Evidence quotes stay attached to
the independently extracted requirements instead of being inferred from its DAG.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_COMPARISONS = {
    "严格大于": ">", "严格超过": ">", "严格小于": "<",
    "小于或等于": "<=", "大于或等于": ">=",
    "不超过": "<=", "至多": "<=", "最多": "<=", "不高于": "<=", "不大于": "<=", "小于等于": "<=", "≤": "<=", "<=": "<=",
    "不少于": ">=", "不低于": ">=", "不小于": ">=", "至少": ">=", "达到": ">=", "大于等于": ">=", "≥": ">=", ">=": ">=",
    "超过": ">", "高于": ">", "大于": ">", ">": ">",
    "低于": "<", "少于": "<", "小于": "<", "<": "<",
    "等于": "=", "为": "=", "是": "=", "==": "=", "=": "=",
}
_COMPARATOR = "|".join(re.escape(x) for x in sorted(_COMPARISONS, key=len, reverse=True))
_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_EXCLUSION = re.compile(r"(?:排除|剔除|去掉|删除)(?:\s|所有|全部|那些|其中|满足|符合)*[‘’“\"']?\s*$")
_POSTFIX_EXCLUSION = re.compile(
    r"^\s*(?:(?:的)?(?:全部|所有)?(?:房源|住宅|房子|记录)\s*(?:全部|都)?(?:请)?(?:排除|剔除|去掉|删除)(?:掉)?|"
    r"的(?:房源|住宅|房子|记录)?\s*(?:也|都)?(?:不要|不保留|不选))"
    r"(?=\s*(?:$|后|并|再|然后|接着|即可|就行))")
_COMPLEMENTS = {'>': '<=', '>=': '<', '<': '>=', '<=': '>', '=': '!='}


def _numbered_sections(text: str) -> tuple[str, list[tuple[int, str]]]:
    markers = list(re.finditer(r"(?:^|(?<=[。；;：:\n]))\s*(?:(?P<plain>[1-9]\d?)[.、)](?!\d)|[（(](?P<bracket>[1-9]\d?)[）)]|(?P<han>[一二三四五六七八九十]{1,3})、)\s*", text))
    def number(marker):
        raw = marker['plain'] or marker['bracket'] or marker['han']
        if raw.isascii():
            return int(raw)
        digits = {char: index for index, char in enumerate('零一二三四五六七八九')}
        if '十' in raw:
            high, low = raw.split('十', 1)
            return digits.get(high, 1) * 10 + digits.get(low, 0)
        return digits.get(raw, 0)
    return (text[:markers[0].start()].strip() if markers else text,
            [(number(marker), text[marker.end():markers[index + 1].start() if index + 1 < len(markers) else len(text)].strip())
             for index, marker in enumerate(markers)])


def _instruction_parts(text: str) -> tuple[list[str], str | None]:
    """Recognize bounded explicit lists; numbering alone does not establish dataflow."""
    sections = _numbered_sections(text)[1]
    parts = [part for _, part in sections]
    if len(parts) < 2:
        return [], None
    if len(parts) > 8:
        return [], "请将本次明确编号的分析任务限制在8项以内，或拆分为多次请求。"
    indices = [index for index, _ in sections]
    if len(indices) != len(set(indices)) or indices != sorted(indices):
        return [], "任务编号存在重复或顺序冲突，请用唯一且递增的编号明确步骤顺序。"
    if re.search(r"按顺序|依次|顺次|逐步|\bin order\b|\bsequential", text, re.I) or any(re.search(
        r"上一步|前一步|上述|前述|同一.{0,6}结果|(?:缓冲|筛选|重投影|处理)结果|然后|接着|继续|\bprevious\b|\bthen\b", part, re.I)
        for part in parts[1:]):
        return [], None
    anchored = all(re.search(r"^(?:请)?(?:分别|独立)?(?:对|为|把|将).{0,20}(?:原始|上传|附件|地块|站点|房源|图层|栅格|数据|建筑|学校|每个|每块)|^(?:for|on|using|from)\b", part, re.I)
                   for part in parts[1:])
    if anchored or re.search(r"分别独立|相互独立|独立任务|\bindependent\b", text, re.I):
        return parts, None
    return [], "请明确编号任务之间的关系：后续各项处理原始输入，还是按顺序处理上一项的结果？"


def _fields(artifacts: Sequence[Mapping[str, Any]]) -> set[str]:
    result: set[str] = set()
    for artifact in artifacts:
        schema = artifact.get("schema")
        if not isinstance(schema, Mapping):
            continue
        for metadata in (schema, schema.get("inspection", {})):
            if not isinstance(metadata, Mapping):
                continue
            fields = metadata.get("fields", {})
            if isinstance(fields, Mapping):
                result.update(str(name) for name in fields)
            elif isinstance(fields, (list, tuple)):
                result.update(name for name in fields if isinstance(name, str))
    return result


def _positive_clauses(text: str) -> str:
    # Keep boundaries: a negated operation must not erase the next requested one.
    text = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    def remove_negated_action(match):
        # A postfix row exclusion is part of its predicate, not an instruction
        # to skip the operation. Preserve its original evidence verbatim.
        if (match.group().strip() == '不要' and re.search(
                r"的(?:房源|住宅|房子|记录)?\s*(?:也|都)?$", text[:match.start()])):
            return match.group()
        return ''
    text = re.sub(r"(?:但|而)?(?:不要|无需|不用|不必|禁止|不做|不保证|不需要|不考虑)[^，,。；;!！?？\n]*", remove_negated_action, text)
    text = re.sub(r"(?:^|(?<=[，,。；;]))\s*(?:请)?别[^，,。；;!！?？\n]*", "", text)
    # Preserve the user's delimiters. Flattening full stops into commas lets
    # an action in one sentence acquire the object of an unrelated next one.
    clauses = re.split(r"([，,。；;!！?？\n])", text)
    return "".join(clause for clause in clauses if not re.match(
        r"\s*(?:请)?(?:不要|无需|不用|别|不必|禁止|不做|不保证|不需要|不考虑)", clause))


def numeric_conditions(text: str, fields: set[str], field_units: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Bind literal inspected column names; never guess columns or their units."""
    found: list[tuple[int, dict[str, Any]]] = []
    occupied: list[tuple[int, int]] = []
    for name in sorted(fields, key=lambda value: (-len(value), value)):
        pattern = re.compile(re.escape(name) + r"[”’\"']?\s*(?:字段|列)?\s*(?:要|需|必须)?\s*"
            r"(?:(?P<comparison>" + _COMPARATOR + r")|(?P<bound>上限|下限)\s*(?:改为|为|是)|(?P<within>在))\s*"
            r"(?P<value>" + _NUMBER + r")\s*(?P<unit>亿元|万元|元|万|平方米|平米|㎡|公顷|平方公里)?(?(within)\s*以内)")
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in occupied):
                continue
            occupied.append(match.span())
            value = float(match['value'])
            unit = match['unit']
            operator = (_COMPARISONS[match['comparison']] if match['comparison'] else
                        '>=' if match['bound'] == '下限' else '<=')
            end = match.end()
            boundary_states = set()
            comparison_ambiguous = False
            if match['bound']:
                # Only adjacent annotations belonging to this bound can change
                # its endpoint. Never borrow another field's boundary policy.
                annotation = re.compile(r'\s*(?:[，,；;]\s*(?:但|而)?\s*|(?P<opening>[（(])\s*)'
                    r'(?P<inclusion>不包含|不含|包含|含)\s*(?P<label>上限|下限|边界值|边界|等号)(?(opening)\s*[）)])')
                while boundary := annotation.match(text, end):
                    comparison_ambiguous |= boundary['label'] not in {match['bound'], '边界值', '边界', '等号'}
                    boundary_states.add(boundary['inclusion'].startswith('不'))
                    end = boundary.end()
                comparison_ambiguous |= len(boundary_states) > 1
                if boundary_states == {True}:
                    operator = operator[0]
            unit_ambiguous = False
            declared = (field_units or {}).get(name, name)
            # Unit conversion is allowed only when the physical column itself
            # explicitly declares the same dimension; never assume price units.
            if "万元" in declared and unit in {"元", "万元", "万", "亿元"}:
                value *= {"元": .0001, "万元": 1, "万": 1, "亿元": 10000}[unit]
            elif "元" in declared and "万元" not in declared and unit in {"元", "万元", "万", "亿元"}:
                value *= {"元": 1, "万元": 10000, "万": 10000, "亿元": 100000000}[unit]
            elif ("平方米" in declared or "平米" in declared) and unit in {"公顷", "平方公里"}:
                value *= {"公顷": 10000, "平方公里": 1000000}[unit]
            elif unit and not (unit in declared or unit in {"平米", "㎡"} and "平方米" in declared):
                unit_ambiguous = True
            prefix = re.split(r"[，,。；;!！?？\n]", text[:match.start()])[-1]
            suffix = re.split(r"[，,。；;!！?？\n]", text[end:], maxsplit=1)[0]
            exclusion = _EXCLUSION.search(prefix)
            postfix_exclusion = _POSTFIX_EXCLUSION.match(suffix)
            excluded = bool(exclusion or postfix_exclusion)
            found.append((match.start(), {"op": "attribute.filter", "params": {
                "field": name, "operator": operator, "value": value,
                "negate": excluded},
                "evidence": (exclusion.group() if exclusion else "") + text[match.start():end]
                            + (postfix_exclusion.group() if postfix_exclusion else ""),
                **({"polarity": "exclude"} if excluded else {}),
                **({"polarity_ambiguous": True} if not excluded and re.search(r"排除|剔除|去掉|删除", prefix + suffix) else {}),
                **({"comparison_ambiguous": True} if comparison_ambiguous else {}),
                **({"unit_ambiguous": True} if unit_ambiguous else {})}))
    return [item for _, item in sorted(found, key=lambda item: item[0])]


def _null_rejections(text: str, fields: set[str]) -> tuple[list[dict[str, Any]], str, set[str]]:
    """Prove NOT(null(A) OR null(B)) as separate non-null obligations."""
    if not fields:
        return [], text, set()
    literal = '(?:' + '|'.join(re.escape(name) for name in sorted(fields, key=len, reverse=True)) + ')'
    field = rf'[“‘\"\']?{literal}[”’\"\']?'
    pattern = re.compile(rf'^\s*(?P<fields>{field}(?:\s*(?:或者|或)\s*{field})*)\s*'
                         r'(?:缺失|为空|是空值)(?:的)?(?:房源|住宅|房子|记录)?\s*(?:也|都)?(?P<action>不保留|不要|不选|保留)\s*$')
    conditions, remaining = [], []
    seen, retained = set(), set()
    for clause in re.split(r'([，,。；;!！?？\n])', text):
        match = pattern.fullmatch(clause)
        if not match:
            remaining.append(clause)
            continue
        if match['action'] == '保留':
            retained.update(re.findall(literal, match['fields']))
            remaining.append(clause)
            continue
        for name in re.findall(literal, match['fields']):
            if name not in seen:
                seen.add(name)
                conditions.append({'op': 'attribute.filter', 'params': {
                    'field': name, 'operator': 'is_null', 'value': None, 'negate': True},
                    'evidence': clause, 'null_rejection': True})
    return conditions, ''.join(remaining), seen & retained


def _buffer_action_text(clause: str) -> str:
    # A verb inside a requested layer/column name is data, not an action.
    clause = re.sub(r"(?:图层(?:名称|名)?|字段名|列名|标题)\s*(?:重命名为|命名为|改为|改成|为|是|叫).*$", "", clause)
    return re.sub(r"(?:已有|现有|原有)(?:缓冲(?:区|面|带)|服务范围面?|影响区面?)"
                  r"(?:图层)?的?(?:统计表|说明|属性|报告)", "", clause)


def _buffer_action_clauses(text: str) -> list[str]:
    """Recognize bounded spatial actions, excluding names and existing layers."""
    text = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text)
    action = re.compile(
        r"(?:做|生成|计算|建立|创建|求|画出?|圈出|算出|制作|添加).{0,32}"
        r"(?:缓冲|周边范围面|范围面|服务区面?|服务范围面?|影响区面?|半径.{0,16}多边形)|"
        r"(?:逐点|逐个|逐一|分别|先|再|继续)缓冲|"
        r"(?:向外扩展|向外扩大|外扩|扩成|扩为).{0,24}"
        r"(?:范围面|影响区面?|缓冲|多边形|形成面)|"
        r"画出.{0,8}距离.{0,20}区域|"
        r"(?:我要|想看|需要).{0,16}(?:选中|筛选|每处|每个|各个).{0,20}"
        r"(?:站点|点位|设施).{0,16}(?:服务区|服务范围)|"
        r"\b(?:create|compute|make)\s+(?:a\s+)?buffer\b", re.I)
    result = []
    for clause in re.split(r"[，,。；;!！?？\n]", text):
        clause = _buffer_action_text(clause)
        if action.search(clause):
            result.append(clause)
    return result


def _buffer_distance_context(text: str, *, answer: bool = False) -> str:
    """Do not let an unrelated road distance or elevation become a radius."""
    from .llm_planner import _DISTANCE_WITH_UNIT
    positive = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", _positive_clauses(text))
    clauses = _buffer_action_clauses(positive)
    for clause in re.split(r"[，,。；;!！?？\n]", positive):
        if re.search(r"半径|(?:缓冲|影响|服务)距离|覆盖周边", clause):
            clauses.append(clause)
    # Action context alone is insufficient: a selected station can itself be
    # 100 m from a road or have elevation 100 m. Retain only quantities bound
    # to the buffer/radius action, while leaving multiple alternatives intact.
    distances = []
    for clause in clauses:
        previous_end = 0
        for match in _DISTANCE_WITH_UNIT.finditer(clause):
            prefix = clause[:match.start()]
            measurement_prefix = clause[previous_end:match.start()]
            previous_end = match.end()
            if re.search(r"(?:海拔|高程|标高|(?<!距)离|距(?:离)?(?:道路|公路|河流|车站|学校)).{0,16}$", measurement_prefix):
                continue
            if (re.search(r"(?:半径|(?:缓冲|影响|服务)距离|周边|向外扩展|向外扩大|外扩|扩成|扩为|"
                          r"做|生成|计算|建立|创建|求|画出?|圈出|算出|制作|添加|缓冲|以|按|用|距离|"
                          r"\b(?:buffer|radius)\b).{0,16}$", prefix, re.I)
                    or re.match(r"的?(?:缓冲|服务区|影响区)", clause[match.end():])):
                distances.append(match.group())
    if answer:
        matches = list(_DISTANCE_WITH_UNIT.finditer(positive))
        if len(matches) == 1:
            remainder = positive[:matches[0].start()] + positive[matches[0].end():]
            if re.fullmatch(r"[\s，,。；;]*(?:(?:距离|改为|改成|确定为|设为|使用|就是|用|按|为)\s*)*[\s，,。；;]*", remainder):
                distances.append(matches[0].group())
    # A comma immediately before digits is deliberately rejected by the unit
    # parser as a malformed thousands separator; keep candidates separate.
    return "；".join(distances)


def _scoped_buffer_stages(positive_goal: str, *, allow_explicit_single: bool = False
                          ) -> tuple[list[dict[str, Any]], str | None, int]:
    """Prove one complete chain, including radius-defined surface actions.

    An incomplete chain must remain an explicit question, not become an empty
    requirement set that a draft omitting every buffer could satisfy.
    """
    from .llm_planner import PlannerRequest, _verified_buffer_distance_m

    question = '请明确全部缓冲步骤的输入、方向、距离及单位，并按执行顺序重述；不能遗漏独立步骤或把未绑定的输入默认当作上一步结果。'
    text = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", positive_goal)
    surface_action = re.compile(
        r"(?:生成|制作).{0,24}周边.{0,16}的?面|"
        r"按.{0,16}(?:米|公里|千米)生成面|"
        r"缓冲(?=[零〇一二两三四五六七八九十百千万\d])|外扩|扩展|向外扩大")
    previous = re.compile(
        r"(?:对|把|将|基于|以)\s*(?:上一步|前一步)|^(?:上一步|前一步)|"
        r"(?:基于|以)第一步(?:的)?(?:结果|输出)|"
        r"以(?:该|此|这个|上述)(?:面|范围面|多边形)为(?:上一步|前一步)(?:的)?输入")
    chains, current, candidates = [], [], []
    orphan_previous = invalid_direction = False
    for clause in (_buffer_action_text(part.strip()) for part in re.split(r"[，,。；;!！?？\n]", text)):
        if not (_buffer_action_clauses(clause) or surface_action.search(clause)):
            if len(current) > 1:
                chains.append(current)
            current = []
            continue
        # Positive buffer distances must not silently implement an inward
        # contraction or an operation on a future/original input.
        candidates.append(clause)
        if re.search(r"向内|内缩|缩小|收缩|不(?:扩展|外扩|扩大|缓冲|生成|制作)", clause):
            invalid_direction = True
        is_previous = previous.search(clause)
        if current and is_previous and not (len(current) > 1 and "第一步" in clause):
            current.append(clause)
        else:
            if len(current) > 1:
                chains.append(current)
            current = [] if is_previous else [clause]
            orphan_previous |= bool(is_previous)
    if len(current) > 1:
        chains.append(current)
    explicit_single = bool(allow_explicit_single and len(candidates) == 1
        and not invalid_direction and not orphan_previous
        and not re.search(r"下一步|后一步", candidates[0])
        and re.search(r"(?:对|为|把|将|以|基于)\s*(?:原始|上传|筛选|选中)", candidates[0]))
    # Rephrasing one desired service area is not a sequence. A preceding
    # "first filter" clause must not supply ordering for unrelated buffers.
    if not explicit_single and not re.search(r"先|再|然后|随后|上一步|前一步|第一步|第二步|下一步|连续|两次|两步", '，'.join(candidates)):
        return [], None, len(candidates)
    if invalid_direction:
        return [], question, len(candidates)
    if not explicit_single and len(candidates) < 2 and not orphan_previous:
        return [], None, len(candidates)
    # Count every action, including a standalone action before or after the
    # recognized chain. A valid subchain is not proof of the whole request.
    if not explicit_single and (orphan_previous or len(chains) != 1 or not 2 <= len(chains[0]) <= 8
            or chains[0] != candidates):
        return [], question, len(candidates)
    result = []
    for index, clause in enumerate(candidates, 1):
        distance = _verified_buffer_distance_m(PlannerRequest(
            run_id="goal-surface-buffer-stage", input_revision=1, user_goal=clause,
            input_artifacts=(), revision_patch={}))
        requirement: dict[str, Any] = {"op": "vector.buffer", "evidence": clause}
        if distance is not None:
            requirement['params'] = {'distance_m': distance}
        result.append({'instruction_index': index, 'requirements': [requirement]})
    return result, None, len(candidates)


def build_goal_requirements(goal: str, artifacts: Sequence[Mapping[str, Any]] = (),
                            revision_patch: Mapping[str, Any] | None = None) -> dict[str, Any]:
    patch = revision_patch or {}
    answer = patch.get("clarification")
    answer = answer if isinstance(answer, str) else ""
    positive = _positive_clauses(goal + "，" + answer)
    fields = _fields(artifacts)
    field_units = {}
    for name in fields:
        declared = re.findall(re.escape(name) + r"[”’\"']?\s*(?:字段|列)?的?\s*单位\s*(?:是|为|改为)\s*(万元|元|平方米|平米)", positive)
        if declared:
            field_units[name] = declared[-1]
    conditions = numeric_conditions(_positive_clauses(goal), fields, field_units)
    revised = numeric_conditions(_positive_clauses(answer), fields, field_units)
    # A later explicit correction replaces that column's old facts; unmentioned
    # constraints and the original goal remain intact for provenance.
    def bound_key(condition):
        params = condition["params"]
        op = params["operator"]
        return params["field"], "upper" if op in {"<", "<="} else "lower" if op in {">", ">="} else op
    revised_bounds = {bound_key(c) for c in revised}
    conditions = [c for c in conditions if bound_key(c) not in revised_bounds] + revised
    null_conditions, boolean_text, null_conflicts = _null_rejections(positive, fields)
    null_fields = {condition['params']['field'] for condition in null_conditions}
    for condition in conditions:
        if condition['params']['negate'] and condition['params']['field'] in null_fields:
            condition['exclude_null'] = True
    conditions += null_conditions
    operations: list[dict[str, Any]] = []

    def require(op: str, pattern: str) -> None:
        match = re.search(pattern, positive, re.IGNORECASE)
        if match and not any(item["op"] == op for item in operations):
            operations.append({"op": op, "evidence": match.group()})

    # Explicit action words, not tokens in arbitrary file/column names.
    buffer_clauses = _buffer_action_clauses(positive)
    if buffer_clauses:
        operations.append({"op": "vector.buffer", "evidence": buffer_clauses[0]})
        # Reuse the existing bounded unit parser on this exact user clause;
        # independent groups therefore keep distinct distances without guessing.
        from .llm_planner import PlannerRequest, _verified_buffer_distance_m
        distance = _verified_buffer_distance_m(PlannerRequest(run_id="goal-obligations", input_revision=1,
            user_goal=_buffer_distance_context(goal), input_artifacts=tuple(artifacts),
            revision_patch={**patch, "clarification": _buffer_distance_context(answer, answer=True)}))
        if distance is not None:
            next(item for item in operations if item["op"] == "vector.buffer")["params"] = {"distance_m": distance}
    require("attribute.rename_field", r"(?:字段|列).{0,12}(?:重命名|改名|更名|改为)|(?:重命名|改名|更名).{0,16}(?:字段|列)|\brename\s+(?:the\s+)?(?:field|column)\b")
    from .llm_planner import PlannerRequest, _verified_rename_binding
    rename_binding = _verified_rename_binding(PlannerRequest(run_id='goal-obligations', input_revision=1,
        user_goal=goal, input_artifacts=tuple(artifacts), revision_patch=dict(patch)))
    if rename_binding is not None:
        rename = next((item for item in operations if item['op'] == 'attribute.rename_field'), None)
        if rename is None:
            rename = {'op': 'attribute.rename_field', 'evidence': answer or goal}
            operations.append(rename)
        rename['params'] = {'old_name': rename_binding[0], 'new_name': rename_binding[1]}
    # Commas can enumerate field names, but a sentence terminator cannot bind
    # row selection ("only keep rows") to a later rename's "field" noun.
    # An internal dot in e.g. area.km2 is still part of a literal column name.
    field_span = r"(?:(?![。；;!！?？\n]|\.(?:\s|$)).)"
    require("attribute.keep_fields",
            rf"(?:只|仅)(?:需|要)?保留{field_span}{{0,40}}(?:字段|列)|"
            rf"保留{field_span}{{0,30}}(?:字段|列){field_span}{{0,12}}(?:其余|其他){field_span}{{0,8}}(?:删除|去掉)|"
            rf"\bkeep\s+only\b{field_span}{{0,40}}\b(?:fields?|columns?)\b")
    artifact_types = {item.get("schema", {}).get("inspection", {}).get("artifact_type", item.get("type")) for item in artifacts}
    reproject_op = "raster.reproject" if artifact_types == {"Raster"} or re.search(r"栅格.{0,12}重投影|重投影.{0,12}栅格", positive) else "vector.reproject"
    require(reproject_op, r"重投影(?:\s*(?:到|为|至|转换|处理|EPSG)|(?=[，,；;。]|$))|(?:转换|变换|改为|转为|转成).{0,16}(?:坐标系|EPSG\s*[:：])|\breproject\b")
    require("attribute.filter", r"(?:筛选|过滤).{0,30}(?:字段|列|等于|大于|小于|不超过|不少于|至少|至多)|(?:字段|列).{0,20}(?:筛选|过滤)|\bfilter\b.{0,30}\b(?:field|column|where)\b")
    require("vector.point_on_surface", r"(?:面内点|表面点)|(?:标注|标签点).{0,30}(?:里面|内部|面内)|(?:里面|内部|面内).{0,30}(?:标注|标签点)")
    require("map.build", r"(?:在|到|成|为)?地图(?:上|中)?(?:显示|展示|呈现|标出)?|(?:显示|展示|画出).{0,12}地图")
    from .compiler import _requested_export_formats
    formats = _requested_export_formats(answer) or _requested_export_formats(goal)
    download = re.search(r"导出|下载|另存为|\b(?:export|download|save)\b", positive, re.I)
    native_raster_delivery = bool(download and (formats == {'geotiff'} or not formats and re.search(
        r"(?:提供|输出|导出|下载|最终|结果)[^。；;\n]{0,24}(?:栅格|\braster\b|\bgeo[ -]?tiff\b)|"
        r"(?:栅格|\braster\b|\bgeo[ -]?tiff\b)[^。；;\n]{0,24}(?:供下载|下载|导出)", positive, re.I)))
    if native_raster_delivery:
        # Raster-producing contracts publish physical GeoTIFF bytes directly.
        # This is a final-output obligation, not an invented file.export task.
        require('raster.calculate', r"(?:像元|有效值|栅格值|波段)[^。；;\n]{0,24}(?:乘|加|减|除|\*|\+)|栅格计算器|地图代数|\braster\s*[*+/\-]")
    else:
        require("file.export", r"(?:导出|下载|另存为)\s*(?:结果|成果|文件|为|成|geojson|gpkg|geopackage)|(?:导出|下载)(?=[，。\s]|$)")
    if any(item["op"] == "file.export" for item in operations):
        if len(formats) == 1:
            next(item for item in operations if item["op"] == "file.export")["params"] = {"format": next(iter(formats))}
    explicit_housing_location = re.search(
        r'(?:房源|住宅|房子)(?:还)?必须位于[^，,。；;!！?？\n]{0,40}(?:学区|招生)(?:范围|多边形|片区)(?:内部|内)', positive)
    spatial_selection = bool(explicit_housing_location or re.search(r"(?:学区|招生)(?:范围|边界|片区)|(?:房源|住宅|小区).{0,20}(?:范围内|区域内|面内)", positive))
    housing = bool(explicit_housing_location or re.search(r"学区房", positive) or any(
        re.search(r"挑选|筛选|选出|寻找|找|排除|剔除", clause) and re.search(r"房源|住宅|房子", clause)
        for clause in re.split(r"[，,。；;!！?？\n]", positive)))
    missing: list[str] = [f"字段 {c['params']['field']} 的实际存储单位（不能直接将用户单位当作字段单位）"
                          for c in conditions if c.get("unit_ambiguous")]
    if null_conflicts:
        missing.append('请明确字段 ' + '、'.join(sorted(null_conflicts)) + ' 的空值最终保留还是排除；当前说明存在冲突')
    if any(c.get('polarity_ambiguous') for c in conditions):
        missing.append('请明确排除/删除作用于哪个筛选条件或字段，不能将不明确的否定条件当作正向筛选')
    if any(c.get('comparison_ambiguous') for c in conditions):
        missing.append('请明确上限或下限是否包含边界值；当前边界说明存在冲突')
    for clause in re.split(r"[，,。；;!！?？\n]", positive):
        clause_conditions = numeric_conditions(clause, fields, field_units)
        if (len(clause_conditions) > 1 and any(c.get('polarity') == 'exclude' for c in clause_conditions)
                and re.search(r"且|和|并|或|同时|任一|全部", clause)):
            missing.append("请明确组合排除条件：排除同时满足这些条件的记录，还是分别排除满足任一条件的记录；不能擅自改变否定的范围")
    if housing:
        if not artifacts:
            missing.append("可定位的房源数据（含价格等筛选字段）")
        zone_declared = bool(explicit_housing_location or re.search(r"(?:学区|招生)(?:范围|边界|片区)", positive))
        zone_field = any("学区" in name or "招生" in name for name in fields)
        if "学区房" in positive and not zone_declared and not zone_field:
            missing.append("学区判定依据：已上传的招生范围/学区字段，还是仅按离学校的距离筛选；距离近不代表属于学区")
        if zone_declared and len(artifacts) < 2 and not zone_field:
            missing.append("用于空间匹配的招生范围数据及对应学校")
        def has_bound(field_pattern: str, operators: set[str]) -> bool:
            for condition in conditions:
                params = condition['params']
                if not re.search(field_pattern, params['field']):
                    continue
                operator = params['operator']
                if params['negate']:
                    operator = _COMPLEMENTS.get(operator)
                if operator in operators:
                    return True
            return False

        def hide_data_literal(match):
            prefix = re.split(r'[，,。；;!！?？\n]', positive[:match.start()])[-1]
            if re.search(r'(?:字段|列|图层名称|图层名|标题).{0,16}'
                         r'(?:包含|含有|等于|改名为|改为|改成|叫|为|是)\s*$', prefix):
                return ''
            return match.group()

        preferences = re.sub(r'''“[^”]*”|‘[^’]*’|"[^"]*"|'[^']*' ''', hide_data_literal, positive, flags=re.X)
        # An explicit reply may withdraw a qualitative preference. This does
        # not erase independently grounded numeric constraints already present.
        area_preference_withdrawn = any(re.fullmatch(
            r'\s*(?:请)?(?:(?:不考虑|无需考虑|不限制)(?:房源)?面积(?:条件|限制|标准)?|面积(?:条件)?不限)\s*', clause)
            for clause in re.split(r'[，,。；;!！?？\n]', answer))
        if re.search(r"便宜|实惠|价格合适|性价比|价格不贵|(?:价格|总价)低(?:一些|一点)?|预算别太高", preferences) and not has_bound(r"价|预算|金额", {'<', '<=', '='}):
            missing.append("价格上限及房源价格字段的单位")
        if (not area_preference_withdrawn and
                re.search(r"宽敞|面积(?:够大|充裕|大(?:些|一些|一点)|也?别太小)|房子大一点|(?:最低面积|面积下限)(?:还|尚)?(?:没定|没有确定|未确定)", preferences)
                and not has_bound(r"面积", {'>', '>=', '='})):
            missing.append("面积下限及房源面积字段的单位")
        commute_withdrawn = bool(re.search(r"(?:不考虑|不限|无需考虑|不需要考虑).{0,8}(?:通学|通勤|上学|距离|时间)", answer))
        if not commute_withdrawn and re.search(r"方便|近一点|不太远|通勤短", positive) and not re.search(r"\d+\s*(?:米|公里|千米|分钟|小时)", positive):
            missing.append("通学/通勤的起终点、方式以及距离或时间上限")
        if spatial_selection:
            spatial = {"op": "vector.extract_location", "evidence":
                       explicit_housing_location.group() if explicit_housing_location else "按用户指定的范围保留完整房源"}
            if re.search(r"完全位于|必须位于.{0,40}(?:内|内部)|在.{0,30}(?:范围|多边形)内部", positive):
                spatial["params"] = {"predicate": "within"}
            operations.append(spatial)
    # The 或 inside one closed comparison is not a predicate connective.
    boolean_text = re.sub(r'小于或等于|大于或等于', lambda match: match.group().replace('或', ''), boolean_text)
    boolean = "any" if len(conditions) > 1 and re.search(r"或|任一|任意一个|至少一个|\bor\b", boolean_text, re.I) else "all"
    result = {"version": "1", "operations": operations, "conditions": conditions,
            "boolean": boolean, "missing_facts": missing,
            "domain": "housing_selection" if housing else "gis",
            "warning": "空间范围与距离分析不构成入学资格保证。" if housing else None}
    if native_raster_delivery:
        result['native_raster_delivery'] = {'artifact_type': 'Raster', 'format': 'geotiff',
                                            'evidence': download.group()}
    relationship_answer = bool(answer and re.search(r"相互独立|独立任务|分别处理原始|都处理原始|按顺序|依次", answer)
                               and not re.search(r"\d|筛选|过滤|缓冲|改名|重命名|导出|重投影", answer))
    parts, relationship_question = _instruction_parts((answer + "\n" if relationship_answer else "") + goal)
    if relationship_question:
        result["missing_facts"].append(relationship_question)
    elif parts:
        # Group evidence is extracted from authenticated user clauses. It is
        # never a coverage claim supplied by the model about its own plan.
        preamble, sections = _numbered_sections(goal)
        scoped_answers: dict[int, str] = {}
        if answer and not relationship_answer:
            answer_preamble, answer_sections = _numbered_sections(answer)
            indices = [index for index, _ in answer_sections]
            if (answer_sections and len(indices) == len(set(indices)) and
                    set(indices) <= {index for index, _ in sections} and
                    not answer_preamble.strip(' ：:\n')):
                scoped_answers = dict(answer_sections)
            else:
                result["missing_facts"].append("请按原编号分别补充对应任务，明确每项修改的条件；不能把一项补充默认应用到所有独立任务。")
        groups = [build_goal_requirements(preamble + '\n' + part, artifacts,
                  {'clarification': scoped_answers[index]} if index in scoped_answers else {}) for index, part in sections]
        result["instruction_groups"] = [{**group, "instruction_index": index, "evidence": part,
            "requires_original_input": True}
            for (index, part), group in zip(sections, groups)]
        result["conditions"] = [condition for group in groups for condition in group["conditions"]]
        result["missing_facts"] += [fact for group in groups for fact in group["missing_facts"]]
    else:
        sequential_preamble, sequential_sections = _numbered_sections(goal)
        if len(sequential_sections) >= 2:
            answer_preamble, answer_sections = _numbered_sections(answer)
            indices = [index for index, _ in answer_sections]
            valid_scoped_answer = bool(answer_sections and len(indices) == len(set(indices))
                and set(indices) <= {index for index, _ in sequential_sections}
                and not answer_preamble.strip(' ：:\n'))
            if answer_sections and not valid_scoped_answer:
                result['missing_facts'].append('补充内容的编号必须唯一且对应原任务编号，请明确要修改哪一步。')
            scoped_answers = dict(answer_sections) if valid_scoped_answer else {}
            stages = [(index, build_goal_requirements(part, artifacts,
                {'clarification': scoped_answers[index]} if index in scoped_answers else {}))
                for index, part in sequential_sections]
            if valid_scoped_answer:
                # A numbered correction replaces only its own occurrence.
                # Flatten the corrected stages for global coverage without
                # erasing another stage's bound on the same physical field.
                result["conditions"] = numeric_conditions(_positive_clauses(sequential_preamble), fields, field_units) + [
                    condition for _, stage in stages for condition in stage["conditions"]]
            result["ordered_stages"] = [{"instruction_index": index,
                "requirements": [item for item in [*stage['operations'], *stage['conditions']]
                                 if item['op'] not in {'map.build', 'file.export'}]}
                for index, stage in stages
                if any(item['op'] not in {'map.build', 'file.export'}
                       for item in [*stage['operations'], *stage['conditions']])]
            buffer_stages = [item for stage in result['ordered_stages'] for item in stage['requirements']
                             if item['op'] == 'vector.buffer']
            verified_distance = next((item.get('params', {}).get('distance_m') for item in operations
                                      if item['op'] == 'vector.buffer'), None)
            if answer and len(buffer_stages) == 1 and verified_distance is not None:
                # This scalar has already passed the request-fact unit parser.
                # Bind it only to a unique existing buffer stage; never replay
                # the whole answer as extra instructions in every stage.
                buffer_stages[0]['params'] = {**buffer_stages[0].get('params', {}), 'distance_m': verified_distance}
            elif len(buffer_stages) > 1:
                # Distances belong to ordered stage occurrences, not an op-wide
                # scalar. The ordered proof cannot credit two stages to one task.
                for operation in operations:
                    if operation['op'] == 'vector.buffer':
                        operation.pop('params', None)
                if answer and not valid_scoped_answer and verified_distance is not None:
                    result['missing_facts'].append('有多个缓冲步骤，请按原编号说明要修改哪一步的缓冲距离，不能将同一距离默认应用到所有步骤。')
                for stage in result['ordered_stages']:
                    if any(item['op'] == 'vector.buffer' and 'distance_m' not in item.get('params', {})
                           for item in stage['requirements']):
                        result['missing_facts'].append(f"第{stage['instruction_index']}步的缓冲距离及单位")
        elif not sequential_sections:
            # Natural-language previous-result bindings carry the same proof
            # obligations as numbered stages; the model's task count is not
            # evidence that the user requested more than one operation.
            from .llm_planner import _DISTANCE_WITH_UNIT
            natural_stages, scope_question, occurrence_count = _scoped_buffer_stages(_positive_clauses(goal))
            answer_stages, answer_question, _ = _scoped_buffer_stages(_positive_clauses(answer),
                allow_explicit_single=bool(scope_question and occurrence_count == 1))
            complete_answer = (not answer_question and (occurrence_count >= 2 or bool(scope_question) and occurrence_count == 1)
                and len(answer_stages) == occurrence_count
                and all('distance_m' in stage['requirements'][0].get('params', {})
                        for stage in answer_stages))
            # A complete, equally scoped correction may resolve an unbound
            # chain, but cannot delete an original action or unrelated filters.
            if complete_answer:
                natural_stages, scope_question = answer_stages, None
            if scope_question:
                result['missing_facts'].append(scope_question)
            if natural_stages:
                result['ordered_stages'] = natural_stages
                if not any(item['op'] == 'vector.buffer' for item in operations):
                    operations.append({'op': 'vector.buffer',
                        'evidence': natural_stages[0]['requirements'][0]['evidence']})
                for operation in operations:
                    if operation['op'] == 'vector.buffer':
                        operation.pop('params', None)
                # Synthetic occurrence indices are not original user numbers.
                # Do not apply an unscoped answer to every buffer occurrence.
                if _DISTANCE_WITH_UNIT.search(answer) and not complete_answer:
                    result['missing_facts'].append('有多个缓冲步骤，请明确每一步的缓冲距离并重述完整顺序，不能将同一距离默认应用到所有步骤。')
                for stage in natural_stages:
                    if 'distance_m' not in stage['requirements'][0].get('params', {}):
                        result['missing_facts'].append(f"第{stage['instruction_index']}次缓冲的距离及单位")
            elif rename_binding is not None:
                # An inspected, bound rename followed by explicit 先...再...
                # has the same primary-input proof as numbered instructions.
                ordered = re.search(r'先(?P<rename>[^。；;\n]*?)[，,]\s*再(?P<buffer>[^。；;\n]*缓冲[^。；;\n]*)', positive)
                buffer = next((item for item in operations if item['op'] == 'vector.buffer'), None)
                if ordered and buffer is not None and _verified_rename_binding(PlannerRequest(
                        run_id='goal-order', input_revision=1, user_goal=ordered['rename'],
                        input_artifacts=tuple(artifacts), revision_patch={})) is not None:
                    rename = next(item for item in operations if item['op'] == 'attribute.rename_field')
                    result['ordered_stages'] = [
                        {'instruction_index': 1, 'requirements': [rename]},
                        {'instruction_index': 2, 'requirements': [buffer]},
                    ]
                    selection = re.search(r'先(?:只保留|仅保留|筛选)(?P<filter>[^。；;\n]+)[。；;]\s*对(?:选中|筛选后)数据\s*$',
                                          positive[:ordered.start()])
                    if selection:
                        selected_conditions = numeric_conditions(selection['filter'], fields, field_units)
                        if selected_conditions:
                            result['ordered_stages'].insert(0, {'instruction_index': 0,
                                                               'requirements': selected_conditions})
    # Missing radii must pause even when the model omits the buffer entirely
    # or invents a plausible distance. Multi-step/group checks above retain
    # their independently scoped facts and must not acquire a shared radius.
    if not result.get('instruction_groups'):
        buffer_stages = [item for stage in result.get('ordered_stages', [])
                         for item in stage['requirements'] if item['op'] == 'vector.buffer']
        single_buffers = buffer_stages if buffer_stages else [
            item for item in operations if item['op'] == 'vector.buffer']
        if len(single_buffers) == 1 and 'distance_m' not in single_buffers[0].get('params', {}):
            result['missing_facts'].append('缓冲距离及单位（例如半径多少米）')
    return result


def _satisfies(task: Any, requirement: Mapping[str, Any]) -> bool:
    params = dict(task.params)
    if params.get('operator') == '==':
        params['operator'] = '='
    if task.op.split('@')[0] == 'attribute.filter':
        params.setdefault('negate', False)
        expected = requirement.get('params', {})
        if params.get('field') == expected.get('field'):
            if requirement.get('null_rejection') and not params['negate']:
                # These actual handler comparisons reject unknown values; !=
                # and contains deliberately do not qualify as non-null proof.
                if params.get('operator') in {'=', '>', '>=', '<', '<='} and params.get('value') is not None:
                    return True
            if requirement.get('exclude_null') and expected.get('negate'):
                complement = _COMPLEMENTS.get(expected.get('operator'))
                if complement and not params['negate'] and params.get('operator') == complement:
                    return params.get('value') == expected.get('value')
    return task.op.split('@')[0] == requirement['op'] and all(
        params.get(key) == value for key, value in requirement.get('params', {}).items())


def _primary_refs(task: Any) -> list[Any]:
    primary = task.inputs.get('input', task.inputs.get('vector', task.inputs.get('raster')))
    return primary if isinstance(primary, list) else [primary] if primary is not None else []


def validate_goal_requirements(requirements: Mapping[str, Any], tasks: Sequence[Any]) -> None:
    """Check obligations AND that final consumers receive the required dataflow."""
    from .compiler import AwaitingInputDiagnostic, CompileError

    missing = requirements.get("missing_facts", [])
    if missing:
        question = "请补充或明确：" + "；".join(missing) + "。"
        raise AwaitingInputDiagnostic("GOAL_FACTS_REQUIRED", "user_goal", question, {
            "missing_facts": missing, "patch_schema": {"type": "object", "additionalProperties": False,
                "properties": {"clarification": {"type": "string", "minLength": 1, "maxLength": 4000,
                                                    "title": "筛选条件与数据依据", "description": question}},
                "required": ["clarification"]}})
    groups = requirements.get("instruction_groups")
    if groups:
        _validate_independent_groups(groups, tasks)
        return
    if requirements.get("boolean") == "any" and len(requirements.get("conditions", [])) > 1:
        raise CompileError("GOAL_BOOLEAN_UNSUPPORTED", "tasks", "不能将“任一条件”串联成“全部条件”；该请求需要显式并集及去重策略。",
                           {"boolean": "any", "conditions": requirements["conditions"]})
    by_key = {t.task_key: t for t in tasks}
    native_leaves: list[str] = []
    if requirements.get('native_raster_delivery'):
        consumed = {getattr(ref, 'task_key', None) for task in tasks
                    for raw in task.inputs.values() for ref in (raw if isinstance(raw, list) else [raw])}
        terminal = [task for task in tasks if task.task_key not in consumed]
        if not terminal or any(not getattr(task, 'outputs', ()) or any(
            output.artifact_type != 'Raster' or output.min_items < 1 or not output.crs
            for output in task.outputs) for task in terminal):
            raise CompileError('GOAL_DELIVERY_TYPE_MISMATCH', 'tasks',
                               '最终下载必须是满足全部处理条件的有效地理参考栅格，不能用矢量、表格或中间栅格代替。',
                               {'requirement': requirements['native_raster_delivery']})
        native_leaves = [task.task_key for task in terminal]
    obligations: list[set[str]] = []
    for required in [*requirements.get("operations", []), *requirements.get("conditions", [])]:
        matching = set()
        for task in tasks:
            if _satisfies(task, required):
                matching.add(task.task_key)
        if not matching:
            raise CompileError("GOAL_REQUIREMENT_MISSING", "tasks", "计划遗漏或改变了用户明确要求的处理条件。", {"requirement": required})
        if required["op"] not in {"map.build", "file.export"}:
            obligations.append(matching)
    if not obligations:
        return

    def parents(task) -> set[str]:
        result = set()
        for raw in task.inputs.values():
            for ref in raw if isinstance(raw, list) else [raw]:
                key = getattr(ref, "task_key", None)
                if isinstance(key, str):
                    result.add(key)
        return result

    dependencies = {key: parents(task) for key, task in by_key.items()}
    consumed = set().union(*dependencies.values()) if dependencies else set()
    delivery = [t.task_key for t in tasks if t.op.split("@")[0] in {"map.build", "file.export"}] + native_leaves
    leaves = delivery or [key for key in by_key if key not in consumed]
    # Bulk independent requests need separate chains; conjunction checks are
    # limited to explicit combined criteria or a requested processing->delivery.
    if len(obligations) == 1 and not delivery and not requirements.get('ordered_stages'):
        return
    guaranteed_cache: dict[str, set[int]] = {}

    def guaranteed(key: str) -> set[int]:
        if key in guaranteed_cache:
            return guaranteed_cache[key]
        task = by_key[key]
        own = {index for index, candidates in enumerate(obligations) if key in candidates}
        # Follow the actual records, not auxiliary masks/other layers. A merge
        # satisfies a condition only if EVERY contributing branch satisfies it;
        # ancestry union would incorrectly certify OR branches as AND.
        inherited: list[set[int]] = []
        for ref in _primary_refs(task):
            upstream = getattr(ref, "task_key", None)
            inherited.append(guaranteed(upstream) if upstream in by_key else set())
        result = own | (set.intersection(*inherited) if inherited else set())
        guaranteed_cache[key] = result
        return result

    for leaf in leaves:
        ancestors, pending = set(), [leaf]
        while pending:
            key = pending.pop()
            if key in ancestors:
                continue
            ancestors.add(key)
            pending.extend(dependencies.get(key, ()))
        if guaranteed(leaf) != set(range(len(obligations))):
            raise CompileError("GOAL_LINEAGE_MISMATCH", f"tasks.{leaf}.inputs", "最终成果必须沿同一数据流满足所有筛选和处理条件，不能展示原始输入或孤立分支。",
                               {"final_task": leaf, "required_task_groups": [sorted(group) for group in obligations], "ancestors": sorted(ancestors)})
    if requirements.get('ordered_stages'):
        _validate_ordered_stages(requirements['ordered_stages'], by_key, leaves)


def _validate_ordered_stages(stages: Sequence[Mapping[str, Any]], by_key: Mapping[str, Any], leaves: Sequence[str]) -> None:
    """Credit a numbered processing stage only after earlier stages reach its primary input."""
    from .compiler import CompileError
    obligations = [(stage['instruction_index'], item) for stage in stages for item in stage['requirements']]
    prerequisites = {stage: {index for index, (other, _) in enumerate(obligations) if other < stage}
                     for stage, _ in obligations}
    cache: dict[str, set[int]] = {}
    def proved(key):
        if key not in cache:
            task = by_key[key]
            upstream = [proved(ref.task_key) if getattr(ref, 'task_key', None) in by_key else set()
                        for ref in _primary_refs(task)]
            inherited = set.intersection(*upstream) if upstream else set()
            cache[key] = inherited | {index for index, (stage, item) in enumerate(obligations)
                                      if prerequisites[stage] <= inherited and _satisfies(task, item)}
        return cache[key]
    for leaf in leaves:
        requested_buffers = sum(any(item['op'] == 'vector.buffer' for item in stage['requirements']) for stage in stages)
        lineage, pending = set(), [leaf]
        while pending:
            key = pending.pop()
            if key in lineage:
                continue
            lineage.add(key)
            pending.extend(ref.task_key for ref in _primary_refs(by_key[key])
                           if getattr(ref, 'task_key', None) in by_key)
        actual_buffers = sum(by_key[key].op.split('@')[0] == 'vector.buffer' for key in lineage)
        if (proved(leaf) != set(range(len(obligations))) or
                requested_buffers > 0 and actual_buffers != requested_buffers):
            raise CompileError('GOAL_LINEAGE_MISMATCH', f'tasks.{leaf}.inputs',
                               '编号顺序中的处理必须以上一步结果为输入，不能颠倒、遗漏或额外重复缓冲后仍宣称完成全部步骤。',
                               {'final_task': leaf, 'ordered_stages': list(stages)})


def _validate_independent_groups(groups: Sequence[Mapping[str, Any]], tasks: Sequence[Any]) -> None:
    """Match each independent instruction to actual final branches, keeping AND checks intact."""
    from .compiler import CompileError
    by_key = {task.task_key: task for task in tasks}
    parents = {}
    for task in tasks:
        parents[task.task_key] = {ref.task_key for raw in task.inputs.values()
            for ref in (raw if isinstance(raw, list) else [raw]) if getattr(ref, "task_key", None) in by_key}
    consumed = set().union(*parents.values()) if parents else set()
    deliveries = [task for task in tasks if task.op.split("@")[0] in {"map.build", "file.export"}]
    candidates = deliveries or [task for task in tasks if task.task_key not in consumed]
    slots: list[set[str]] = []
    covered = set()
    for group in groups:
        requested_deliveries = [item for item in group.get("operations", []) if item["op"] in {"map.build", "file.export"}]
        checks = {**group, "operations": [item for item in group.get("operations", []) if item["op"] not in {"map.build", "file.export"}]}
        # Preserve a concrete missing-operation diagnostic for bounded repair.
        for obligation in [*group.get("operations", []), *group.get("conditions", [])]:
            if not any(_satisfies(task, obligation) for task in tasks):
                raise CompileError("GOAL_REQUIREMENT_MISSING", "tasks", "计划遗漏了编号任务中明确要求的操作或条件。",
                                   {"instruction_index": group["instruction_index"], "requirement": obligation})
        valid = []
        for candidate in candidates:
            ancestors, pending = set(), [candidate.task_key]
            while pending:
                key = pending.pop()
                if key not in ancestors:
                    ancestors.add(key)
                    pending.extend(parents[key])
            try:
                validate_goal_requirements(checks, [task for task in tasks if task.task_key in ancestors])
            except CompileError:
                continue
            if group.get('requires_original_input'):
                # Only this group's authorized transformations (including its
                # global preamble) may intervene between upload and delivery.
                # Loaders change representation, not the requested dataset.
                allowed = [*checks.get('operations', []), *checks.get('conditions', [])]
                specific_ops = {item['op'] for item in allowed if item.get('params')}
                allowed = [item for item in allowed if item.get('params') or item['op'] not in specific_ops]
                primary_ancestors, pending = set(), [candidate.task_key]
                while pending:
                    key = pending.pop()
                    if key in primary_ancestors:
                        continue
                    primary_ancestors.add(key)
                    pending.extend(ref.task_key for ref in _primary_refs(by_key[key])
                                   if getattr(ref, 'task_key', None) in by_key)
                transforms = [by_key[key] for key in primary_ancestors if by_key[key].op.split('@')[0]
                              not in {'io.load_vector', 'io.load_raster', 'map.build', 'file.export'}]
                # Matching an allowed operation does not authorize repeating
                # it on another independent group's already-processed output.
                unique = {(item['op'], repr(sorted(item.get('params', {}).items()))) for item in allowed}
                if (any(not any(_satisfies(task, item) for item in allowed) for task in transforms) or
                        any(sum(task.op.split('@')[0] == op for task in transforms) >
                            sum(required_op == op for required_op, _ in unique) for op, _ in unique)):
                    continue
            valid.append(candidate)
        for obligation in requested_deliveries or [{"op": None}]:
            choices = {task.task_key for task in valid if (obligation["op"] is None or task.op.split("@")[0] == obligation["op"])
                       and all(dict(task.params).get(key) == value for key, value in obligation.get("params", {}).items())}
            if not choices:
                raise CompileError("GOAL_LINEAGE_MISMATCH", "tasks", "每项独立任务必须交付满足本项全部条件的结果，不能用另一项或原始输入代替。",
                                   {"instruction_index": group["instruction_index"], "requirement": obligation})
            slots.append(choices)
            covered.update(choices)
    # Distinct numbered deliveries cannot all be claimed by one output task.
    assigned: dict[str, int] = {}
    def assign(index, seen):
        for key in sorted(slots[index] - seen):
            seen.add(key)
            if key not in assigned or assign(assigned[key], seen):
                assigned[key] = index
                return True
        return False
    if any(not assign(index, set()) for index in range(len(slots))) or any(task.task_key not in covered for task in deliveries):
        raise CompileError("GOAL_LINEAGE_MISMATCH", "tasks", "编号任务的交付不完整，或包含绕过本项处理的地图/导出分支。")
