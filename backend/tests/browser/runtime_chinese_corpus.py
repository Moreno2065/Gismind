"""Frozen, entirely Chinese browser requests with independently specified inputs.

Ten business families, twenty physical profiles and ten authored phrasings form
2,000 requests. Profiles vary thresholds, distance, arithmetic and geography;
case IDs and oracle parameters are never appended to the user-facing request.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json


VERSION = 'zh-browser-composition-v1'
SEED = 20261014
POINT_FAMILIES = ('buffer', 'sequential_buffers', 'rename_buffer', 'buffer_ambiguous')
VECTOR_FORMATS = ('geojson', 'gpkg', 'zip')
POINT_FORMATS = (*VECTOR_FORMATS, 'kml', 'csv', 'tsv')
FORMAT_NAMES = {
    'geojson': '地理杰森文件', 'gpkg': '地理包文件', 'zip': '形状文件压缩包',
    'kml': '谷歌地球标记文件', 'csv': '逗号分隔表格',
    'tsv': '制表符分隔表格', 'tif': '带地理参考的栅格文件',
}

PHRASES = {
    'buffer': (
        '为筛选后的每个点建立半径{distance_m}米的缓冲区。',
        '以留下的站点为中心，分别向外扩展{distance_m}米形成面。',
        '通过数值筛选的站点，各自覆盖周边{distance_m}米，请生成范围面。',
        '我要筛选结果中每处站点的{distance_m}米服务区。',
        '给符合条件的各个点画出距离不超过{distance_m}米的区域。',
        '筛选完成后，按{distance_m}米的距离逐点计算缓冲面。',
        '将选中的点分别扩为半径{distance_m}米的多边形。',
        '每处保留下来的设施影响距离为{distance_m}米，请算出影响区面。',
        '只处理筛选留下的点，为每个点生成{distance_m}米缓冲带。',
        '请对数值符合条件的站点逐一计算{distance_m}米的圆形缓冲范围。',
    ),
    'sequential_buffers': (
        '先对筛选后的点做{first_distance_m}米缓冲，再对上一步的面做{second_distance_m}米缓冲；两个缓冲步骤都要执行。',
        '筛选之后逐点外扩{first_distance_m}米，随后把上一步缓冲区继续外扩{second_distance_m}米，保留这两个处理步骤。',
        '第一步为选中点建立{first_distance_m}米缓冲面，第二步基于第一步结果再缓冲{second_distance_m}米，两步分别完成。',
        '先生成筛选点周边{first_distance_m}米的面，再以该面为上一步输入扩展{second_distance_m}米，请分别执行两次缓冲。',
        '对通过筛选的站点先缓冲{first_distance_m}米，然后对上一步产生的多边形缓冲{second_distance_m}米；要有两个独立步骤。',
        '需要连续两次缓冲：筛选结果先按{first_distance_m}米生成面，上一步结果再按{second_distance_m}米向外扩大。',
        '请保留完整顺序：选中的点先外扩{first_distance_m}米，上一步的面再外扩{second_distance_m}米，两项操作均须执行。',
        '先制作符合条件站点的{first_distance_m}米范围面，再对上一步范围面添加{second_distance_m}米缓冲，两步都做。',
        '处理分成两步：先给筛选后的点做{first_distance_m}米缓冲，再把上一步的面缓冲{second_distance_m}米。',
        '先以选中点为中心扩展{first_distance_m}米，然后以第一步输出的面为输入继续扩展{second_distance_m}米，两步分别执行。',
    ),
    'rename_buffer': (
        '先把筛选结果中的“名称”字段重命名为“新名”，字段值不变，再生成{distance_m}米缓冲面。',
        '将留下站点的“名称”列标题改为“新名”，保留列值，随后逐点做{distance_m}米缓冲。',
        '完成筛选后，字段“名称”改名为“新名”，然后给每个站点建立{distance_m}米的缓冲区。',
        '对选中数据先更改字段名：把“名称”改成“新名”，再按{distance_m}米生成各站点缓冲面。',
        '请顺序执行两项操作：重命名“名称”字段为“新名”；对处理后的点逐个缓冲{distance_m}米。',
        '保留筛选所得各行，把字段“名称”的列名改为“新名”，之后创建{distance_m}米服务区面。',
        '先为选中点调整列标题，“名称”字段的新标题是“新名”；再对它们做{distance_m}米缓冲。',
        '筛选结果先完成“名称”到“新名”的字段重命名，再生成各自半径{distance_m}米的多边形。',
        '将筛选留下的点图层中“名称”字段改称“新名”，值保持原样，再计算{distance_m}米缓冲面。',
        '请把选中站点的“名称”列重命名为“新名”，接着以{distance_m}米作为距离逐点缓冲。',
    ),
    'housing_multi': (
        '筛选价格不超过{price_max}万元且面积不少于{area_min}平方米的房源。',
        '房源必须同时满足价格至多{price_max}万元、面积至少{area_min}平方米。',
        '只留下价格小于或等于{price_max}万元，并且面积大于或等于{area_min}平方米的房源。',
        '预算上限是{price_max}万元，面积下限是{area_min}平方米，两个边界值都包含，两个条件都满足才保留。',
        '价格高于{price_max}万元的不要，面积低于{area_min}平方米的也不要；价格或面积缺失的房源也不保留。',
        '选房标准为价格不高于{price_max}万元，同时面积不小于{area_min}平方米。',
        '帮我留下价格在{price_max}万元以内、面积达到{area_min}平方米的房源，两个阈值均含等号。',
        '对房源先筛价格不超过{price_max}万元，再从剩余房源筛面积至少{area_min}平方米。',
        '需要价格至多{price_max}万元且面积至少{area_min}平方米的交集房源。',
        '用两个并且连接的条件选房：价格不超过{price_max}万元，面积不少于{area_min}平方米。',
    ),
    'housing_exclude': (
        '请排除价格超过{price_max}万元的房源，价格缺失的房源保留。对剩余房源只保留面积至少{area_min}平方米的记录。',
        '先排除价格大于{price_max}万元的记录，但价格缺失不要删除；随后筛选面积不少于{area_min}平方米的房源。',
        '请剔除价格高于{price_max}万元的房源，保留价格缺失项，再要求面积不小于{area_min}平方米。',
        '先排除价格严格超过{price_max}万元的房源，价格缺失仍保留，再选面积大于或等于{area_min}平方米的部分。',
        '帮我排除价格超过{price_max}万元的房源，价格缺失不要因此删掉；然后仅留面积至少{area_min}平方米的房源。',
        '请排除价格大于{price_max}万元的全部房源，价格缺失的仍然保留；继续筛出面积不少于{area_min}平方米的记录。',
        '先删除价格超过{price_max}万元的房源，保留价格缺失记录，然后从剩余数据选面积至少{area_min}平方米的房源。',
        '请排除价格高于{price_max}万元的部分，保留价格缺失的部分；接着保留面积大于或等于{area_min}平方米的房源。',
        '先剔除价格严格大于{price_max}万元的房源，价格缺失的不删；再筛出面积至少{area_min}平方米的房源。',
        '请排除所有价格超过{price_max}万元的记录，保留价格缺失的房源，后续只要面积不低于{area_min}平方米的房源。',
    ),
    'housing_exclude_postfix': (
        '把价格超过{price_max}万元的房源排除，价格缺失的房源保留。再从剩余房源筛选面积至少{area_min}平方米的记录。',
        '将价格大于{price_max}万元的记录剔除，保留价格缺失项；然后只留面积不少于{area_min}平方米的房源。',
        '价格高于{price_max}万元的房源请排除，价格缺失的不要删，再要求面积不小于{area_min}平方米。',
        '把价格严格超过{price_max}万元的房源去掉，保留价格缺失的房源；继续筛选面积大于或等于{area_min}平方米的记录。',
        '将价格超过{price_max}万元的全部房源排除，价格缺失的记录保留，再留下面积至少{area_min}平方米的房源。',
        '价格大于{price_max}万元的房源都剔除，保留价格缺失项，然后从剩余记录筛面积不少于{area_min}平方米的房源。',
        '把价格高于{price_max}万元的记录删除，价格缺失不要因此丢掉；随后面积必须至少{area_min}平方米。',
        '将价格严格大于{price_max}万元的房源排除，价格缺失的仍保留；对剩余房源要求面积不低于{area_min}平方米。',
        '价格超过{price_max}万元的房源请去掉，价格缺失的保留；随后仅取面积大于或等于{area_min}平方米的记录。',
        '把所有价格超过{price_max}万元的房源剔除，价格缺失的不要删除，然后选面积至少{area_min}平方米的房源。',
    ),
    'housing_ambiguous': (
        '我想找便宜又宽敞的房源，价格上限和面积下限还没有确定，请先问我这两个数值。',
        '请帮我挑价格合适、面积够大的房源，具体价格和面积标准尚未提供，先向我确认。',
        '希望房源既便宜又大一些，但价格预算和最低面积还没定，请补问后再筛选。',
        '想找总价低、空间宽敞的房源，价格到多少和面积要多大尚不明确，请先澄清。',
        '选房时看价格和面积，我只说了便宜、宽敞，请先问价格上限及面积下限。',
        '帮我在这些房源里挑价格便宜、面积大些的，住起来宽敞一点。',
        '预算别太高，房子大一点，请按价格和面积替我挑选合适的房源。',
        '我想要价格实惠、面积充裕的房源，两方面都合适才要。',
        '请帮我找价格不贵、面积也别太小的房子。',
        '从附件挑性价比合适的房源，希望价格低一些、面积大一些。',
    ),
    'buffer_ambiguous': (
        '我想给筛选后的点做缓冲，但半径尚未确定，请先问我要多少米。',
        '通过筛选的站点需要生成周边范围面，具体缓冲距离还没提供，请先确认。',
        '为留下的点做缓冲区之前，请先询问缺失的半径，不要自行定距离。',
        '想看选中站点的服务范围，服务半径未指定，先问距离再处理。',
        '筛选结果还要做点缓冲，缓冲距离暂时没有，请向我补问。',
        '给符合条件的站点做个适当大小的缓冲面，范围别太远。',
        '我想看选中站点周边的服务范围，圈出一个合适的缓冲区。',
        '对筛选留下的站点分别做一圈缓冲带，覆盖附近就行。',
        '请把符合数值条件的站点扩成影响区面，大小适中。',
        '为筛选后的每个站点生成周边缓冲区，不用扩得太远。',
    ),
    'vector_reproject_filter': (
        '先保留数值严格大于{threshold}的点，再把筛选结果从网页墨卡托投影实际转换为世界大地经纬度。',
        '筛出数值超过{threshold}的记录，再将其米制投影坐标重投影到世界大地坐标系的经度纬度。',
        '仅取数值大于{threshold}的站点，随后从网页墨卡托转换成世界大地经纬度坐标。',
        '先筛数值大于{threshold}且不含等号的要素，再把它们重投影到世界大地经纬度。',
        '保留数值严格超过{threshold}的点位，将所得网页墨卡托坐标实际换算成世界大地经纬度。',
        '请先按数值大于{threshold}过滤记录，然后把筛选所得点转为世界大地坐标系的经纬度。',
        '对附件先做数值筛选，只留数值超过{threshold}的点，再从米制网页墨卡托转换到世界大地经纬度。',
        '按数值严格大于{threshold}选出记录，之后把坐标投影换为世界大地经纬度，必须实际变换坐标。',
        '留下数值大于{threshold}的部分，接着由网页墨卡托投影重投影为世界大地经纬度数据。',
        '先选择数值严格超过{threshold}的站点，随后输出这些站点的世界大地经纬度版本。',
    ),
    'raster_calculate': (
        '每个有效像元先乘{factor}，再加{offset}，无数据像元仍为无数据。',
        '请对有效栅格值先扩大为原来的{factor}倍，再增加{offset}，保持无数据掩膜。',
        '栅格运算顺序是先乘{factor}后加{offset}，只处理有效像元，无数据位置不要填值。',
        '将所有有效像元值乘以{factor}，随后统一加上{offset}；原来的无数据像元不变。',
        '我需要原栅格有效值的{factor}倍再加{offset}，无数据仍保留无数据。',
        '先把有效像元乘{factor}，然后给乘积加{offset}，不要改变无数据区域。',
        '对每个有效栅格值计算原值乘{factor}再加{offset}，无数据掩膜原样保留。',
        '有效像元需要先乘以{factor}再增加{offset}，无数据像元不参与算术。',
        '生成新栅格：有效像元取原值的{factor}倍后加{offset}，无数据位置依旧为空。',
        '请按先乘后加处理栅格，有效值乘{factor}再加{offset}，无数据保持原样。',
    ),
}
FAMILIES = tuple(PHRASES)
VECTOR_DELIVERY = '在地图上展示最终结果，并将同一结果导出为地理包空间数据库文件供下载。保留未要求修改的原始属性。'
HOUSING_SPATIAL = '房源还必须位于第二个附件的学区多边形内部，落在边界上的房源不选。学区仅作空间筛选，不承诺入学资格。'


@dataclass(frozen=True)
class BrowserCase:
    case_id: str
    family: str
    profile: int
    paraphrase: int
    prompt: str
    clarification_reply: str | None
    clarification_terms: tuple[str, ...]
    upload_format: str
    params: dict


def _parameters(profile: int, family: str) -> dict:
    return {
        'seed': SEED + profile,
        'distance_m': 100 + 25 * profile,
        'first_distance_m': 25 + 5 * profile,
        'second_distance_m': 75 + 20 * profile,
        'threshold': 10 + 2 * profile,
        'price_max': 200 + 10 * profile,
        'area_min': 60 + 2 * profile,
        'factor': profile + 2,
        'offset': profile + 1,
        'source_epsg': 3857 if family in {'vector_reproject_filter', 'raster_calculate'} else 4326,
        'target_epsg': 3857 if family == 'raster_calculate' else 4326,
        'origin_lon': round(118.70 + profile * .01, 2),
        'origin_lat': round(32.00 + profile * .005, 3),
    }


def load_cases() -> tuple[BrowserCase, ...]:
    cases = []
    for family, phrases in PHRASES.items():
        for profile in range(20):
            params = _parameters(profile, family)
            formats = ('tif',) if family == 'raster_calculate' else POINT_FORMATS if family in POINT_FAMILIES else VECTOR_FORMATS
            upload_format = formats[profile % len(formats)]
            location = f"附件位于东经{params['origin_lon']:.2f}度、北纬{params['origin_lat']:.3f}度附近。"
            context = f'第一个附件为{FORMAT_NAMES[upload_format]}。' + location
            if family.startswith('housing_'):
                context += '房源的“价格”字段单位为万元，“面积”字段单位为平方米；第二个附件是学区范围。'
            elif family == 'raster_calculate':
                context += '栅格采用网页墨卡托投影，标准代码3857。'
            else:
                context += '站点有编号、名称、数值、类别属性。'
                context += '原始坐标采用网页墨卡托投影，标准代码3857，单位米。' if params['source_epsg'] == 3857 else '原始坐标采用世界大地坐标系一九八四（标准代码4326）的经纬度。'
                if upload_format in {'csv', 'tsv'}:
                    context += '请用“经度”“纬度”两列生成点，坐标列可以作为原属性保留。'
                if family in POINT_FAMILIES:
                    context += f"先只保留数值严格大于{params['threshold']}的站点，等于阈值的不要。"
            for paraphrase, phrase in enumerate(phrases):
                prompt = context + phrase.format(**params)
                reply, terms = None, ()
                if family.startswith('housing_'):
                    prompt += HOUSING_SPATIAL
                    if family == 'housing_multi':
                        prompt += '价格或面积缺失的房源不保留。'
                if family == 'raster_calculate':
                    prompt += '保留原始坐标系、像元网格和无数据定义，提供最终带地理参考的栅格文件供下载。'
                else:
                    if family == 'vector_reproject_filter':
                        prompt += '目标为世界大地坐标系一九八四，标准代码4326。'
                    prompt += VECTOR_DELIVERY
                if family == 'housing_ambiguous':
                    terms = ('价格', '预算', '总价', '价位', '面积', '平方米')
                    reply = (f"价格上限为{params['price_max']}万元，包含上限；面积下限为{params['area_min']}平方米，包含下限。"
                             '必须同时满足价格与面积条件，价格或面积缺失的房源不保留。'
                             + HOUSING_SPATIAL + VECTOR_DELIVERY)
                elif family == 'buffer_ambiguous':
                    terms = ('距离', '半径', '多少米', '多远', '米数')
                    reply = (f"缓冲半径确定为{params['distance_m']}米。对之前数值严格大于{params['threshold']}的站点逐个缓冲，"
                             '不同站点的面不要合并。' + VECTOR_DELIVERY)
                elif family in POINT_FAMILIES:
                    prompt += '不同站点的面不要合并。'
                cases.append(BrowserCase(
                    f'浏览器-{family}-{profile:02d}-{paraphrase:02d}', family, profile, paraphrase,
                    prompt, reply, terms, upload_format, dict(params),
                ))
    return tuple(cases)


def corpus_digest(cases=None) -> str:
    if cases is None:
        cases = load_cases()
    raw = '\n'.join(json.dumps(asdict(case), ensure_ascii=False, sort_keys=True, separators=(',', ':')) for case in cases)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


CORPUS_SHA256 = 'c9b24017501971695a9d3ae8cf9c692eb33dda0ae4c7ac25acbd703ce3de0316'
