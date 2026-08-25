"""Authored 200-prompt multilingual convergence matrix.

Each family expresses one public GIS purpose in six distinct natural languages
(simplified/traditional Chinese, English, Spanish, Japanese and French).  The
last eight prompts exercise valid edge inputs.  This module is data-only: the
runner still talks to the public HTTP API and never imports backend code.
"""
from __future__ import annotations

from dataclasses import dataclass, field


LANGUAGES = ("zh", "zh_tw", "en", "es", "ja", "fr")


@dataclass(frozen=True)
class PromptCase:
    id: str
    intent: str
    language: str
    prompt: str
    expected_tools: dict[str, int]
    upload_names: tuple[str, ...] = ()
    expect_map: bool = False
    boundary: bool = False
    session_group: str | None = None


# Agent-visible, schema-backed public capabilities.  Internal toolkit/skill
# administration hooks and handlers not exposed to any Agent role are excluded.
PUBLIC_AGENT_TOOLS = frozenset({
    "geo_code", "geo_transform", "query_poi", "data_io_read", "buffer",
    "overlay", "voronoi", "isochrone", "map_layer_build", "clip_layer",
    "dissolve_layer", "merge_layers", "join_by_location", "join_by_nearest",
    "count_points_in_polygon", "extract_by_location", "convex_hull",
    "bounding_boxes", "centroid_layer", "point_on_surface",
    "simplify_geometry", "fix_geometries", "check_validity",
    "reproject_layer", "extract_by_attribute", "field_calculator", "slope",
    "aspect", "hillshade", "zonal_statistics", "reclassify_raster",
    "export_result",
})


def _variants(
    zh: str, zh_tw: str, en: str, es: str, ja: str, fr: str,
) -> dict[str, str]:
    return {"zh": zh, "zh_tw": zh_tw, "en": en, "es": es, "ja": ja, "fr": fr}


FAMILIES = (
    (
        "geocode", {"geo_code": 1}, (), False,
        _variants(
            "请告诉我南京新街口的经纬度坐标。",
            "請提供南京新街口的經緯度座標。",
            "What are the latitude and longitude coordinates of Xinjiekou in Nanjing?",
            "Dame las coordenadas de latitud y longitud de Xinjiekou en Nankín.",
            "南京の新街口の緯度経度座標を教えてください。",
            "Donne-moi les coordonnées latitude-longitude de Xinjiekou à Nankin.",
        ),
    ),
    (
        "coordinate_transform", {"geo_transform": 1}, (), False,
        _variants(
            "将南京新街口的 WGS84 坐标 118.7782,32.0417 转换为 GCJ02。",
            "南京新街口的 WGS84 座標 118.7782,32.0417 請轉成 GCJ02。",
            "Convert the WGS84 coordinate 118.7782, 32.0417 for Nanjing Xinjiekou to GCJ02.",
            "Convierte la coordenada WGS84 118.7782, 32.0417 de Xinjiekou a GCJ02.",
            "南京新街口の WGS84 座標 118.7782,32.0417 を GCJ02 に変換してください。",
            "Convertis la coordonnée WGS84 118.7782, 32.0417 de Xinjiekou vers GCJ02.",
        ),
    ),
    (
        "poi_query", {"geo_code": 1, "query_poi": 1}, (), True,
        _variants(
            "查询南京新街口 500 米内有多少家蜜雪冰城。",
            "查詢南京新街口五百公尺內有多少家蜜雪冰城。",
            "Find Mixue stores within 500 metres of Xinjiekou, Nanjing.",
            "Busca tiendas Mixue a menos de 500 metros de Xinjiekou, Nankín.",
            "南京の新街口から500メートル以内のMixueを検索してください。",
            "Cherche les magasins Mixue dans un rayon de 500 mètres autour de Xinjiekou à Nankin.",
        ),
    ),
    (
        "metro_buffer", {"geo_code": 1, "query_poi": 1, "buffer": 1, "map_layer_build": 1}, (), True,
        _variants(
            "找出南京夫子庙 1 公里内的地铁站，并绘制 1 公里缓冲区。",
            "找出南京夫子廟一公里內的地鐵站，並繪製一公里緩衝區。",
            "Find metro stations within 1 km of Nanjing Fuzimiao and draw their 1 km buffer.",
            "Encuentra estaciones de metro a 1 km de Fuzimiao en Nankín y dibuja su buffer.",
            "南京の夫子廟から1km以内の地下鉄駅を探し、1kmバッファを描いてください。",
            "Trouve les stations de métro à moins de 1 km de Fuzimiao à Nankin et trace leur zone tampon.",
        ),
    ),
    (
        "coverage_intersection", {"geo_code": 2, "query_poi": 2, "buffer": 2, "overlay": 1, "map_layer_build": 1}, (), True,
        _variants(
            "求南京新街口与夫子庙各 500 米蜜雪冰城覆盖区的交集并显示。",
            "計算南京新街口與夫子廟各五百公尺 Mixue 覆蓋區的交集並顯示。",
            "Show the intersection of the 500 m Mixue coverage areas at Xinjiekou and Fuzimiao in Nanjing.",
            "Muestra la intersección de las coberturas Mixue de 500 m en Xinjiekou y Fuzimiao.",
            "南京の新街口と夫子廟にあるMixueの500m圏の交差部分を表示してください。",
            "Affiche l'intersection des zones de couverture Mixue de 500 m à Xinjiekou et Fuzimiao.",
        ),
    ),
    (
        "voronoi", {"geo_code": 4, "voronoi": 1}, (), True,
        _variants(
            "为中山陵、夫子庙、新街口、玄武湖四个 POI 生成泰森多边形。",
            "為中山陵、夫子廟、新街口、玄武湖四個 POI 產生泰森多邊形。",
            "Build Voronoi polygons for Zhongshanling, Fuzimiao, Xinjiekou and Xuanwu Lake.",
            "Crea polígonos de Voronoi para Zhongshanling, Fuzimiao, Xinjiekou y el lago Xuanwu.",
            "中山陵、夫子廟、新街口、玄武湖の4地点でボロノイ（泰森）多角形を作成してください。",
            "Crée des polygones de Voronoï pour Zhongshanling, Fuzimiao, Xinjiekou et le lac Xuanwu.",
        ),
    ),
    (
        "isochrone", {"geo_code": 1, "isochrone": 1}, (), True,
        _variants(
            "绘制上海人民广场步行 15 分钟可达范围。",
            "繪製上海人民廣場步行十五分鐘可達範圍。",
            "Draw the 15-minute walking isochrone around People's Square in Shanghai.",
            "Dibuja el área alcanzable a pie en 15 minutos desde People's Square en Shanghái.",
            "上海の人民広場から徒歩15分で到達できる等時圏を描いてください。",
            "Trace l'isochrone piétonne de 15 minutes autour de People's Square à Shanghai.",
        ),
    ),
    (
        "map_style", {"data_io_read": 1, "map_layer_build": 1}, ("sample_points",), True,
        _variants(
            "将上传点图层按 class 字段分级设色显示。",
            "將上傳點圖層依 class 欄位做分級設色顯示。",
            "Render the uploaded point layer with a categorical style by its class field.",
            "Muestra la capa de puntos subida con colores categóricos por el campo class.",
            "アップロードしたポイントレイヤーをclass列で分類色表示してください。",
            "Affiche la couche de points importée avec une couleur catégorielle selon le champ class.",
        ),
    ),
    (
        "clip", {"data_io_read": 2, "clip_layer": 1}, ("sample_points", "nanjing_admin"), True,
        _variants(
            "用上传的南京行政区面裁剪上传的 POI 点图层。",
            "用上傳的南京行政區面裁剪上傳的 POI 點圖層。",
            "Clip the uploaded POI point layer with the uploaded Nanjing administrative polygon.",
            "Recorta la capa de puntos POI subida usando el polígono administrativo de Nankín subido.",
            "アップロード済みの南京行政区ポリゴンでPOIポイントレイヤーをクリップしてください。",
            "Découpe la couche de points POI importée avec le polygone administratif de Nankin importé.",
        ),
    ),
    (
        "dissolve", {"data_io_read": 1, "dissolve_layer": 1}, ("parcels",), True,
        _variants(
            "按 region 字段融合上传图层中相邻地块。",
            "依 region 欄位融合上傳圖層中相鄰地塊。",
            "Dissolve adjacent parcels in the uploaded layer by the region field.",
            "Disuelve las parcelas adyacentes de la capa subida por el campo region.",
            "アップロードしたレイヤーの隣接区画をregionフィールドでディゾルブしてください。",
            "Fusionne les parcelles adjacentes de la couche importée selon le champ region.",
        ),
    ),
    (
        "merge", {"data_io_read": 2, "merge_layers": 1}, ("xuanwuhu", "zijinshan"), True,
        _variants(
            "将上传的玄武湖图层与紫金山图层合并为一个图层。",
            "將上傳的玄武湖圖層與紫金山圖層合併為一個圖層。",
            "Merge the uploaded Xuanwu Lake and Zijin Mountain layers into one layer.",
            "Combina las capas subidas del lago Xuanwu y la montaña Zijin en una sola capa.",
            "アップロード済みの玄武湖レイヤーと紫金山レイヤーを1つにマージしてください。",
            "Fusionne les couches importées du lac Xuanwu et de la montagne Zijin en une seule couche.",
        ),
    ),
    (
        "spatial_join", {"data_io_read": 2, "join_by_location": 1}, ("sample_points", "streets"), True,
        _variants(
            "对上传 POI 点和街道面执行 intersects 空间连接。",
            "對上傳 POI 點與街道面執行 intersects 空間連接。",
            "Perform an intersects spatial join between the uploaded POI points and street polygons.",
            "Realiza una unión espacial intersects entre los POI subidos y los polígonos de calles.",
            "アップロード済みPOI点と街道ポリゴンにintersects空間結合を実行してください。",
            "Exécute une jointure spatiale intersects entre les POI importés et les polygones de rues.",
        ),
    ),
    (
        "nearest_join", {"data_io_read": 2, "join_by_nearest": 1}, ("sample_points", "bus_stations"), True,
        _variants(
            "为上传的每个 POI 关联最近公交站。",
            "為上傳的每個 POI 關聯最近公車站。",
            "Join every uploaded POI to its nearest bus stop.",
            "Asocia cada POI subido con su parada de autobús más cercana.",
            "アップロードした各POIを最寄りのバス停に結合してください。",
            "Associe chaque POI importé à son arrêt de bus le plus proche.",
        ),
    ),
    (
        "point_count", {"data_io_read": 2, "count_points_in_polygon": 1}, ("streets", "sample_points"), True,
        _variants(
            "统计上传街道面内包含的上传 POI 点数量。",
            "統計上傳街道面內包含的上傳 POI 點數量。",
            "Count uploaded POI points inside each uploaded street polygon.",
            "Cuenta los puntos POI subidos dentro de cada polígono de calle subido.",
            "各アップロード済み街道ポリゴン内のPOI点数を集計してください。",
            "Compte les POI importés à l'intérieur de chaque polygone de rue importé.",
        ),
    ),
    (
        "extract_by_location", {"data_io_read": 2, "extract_by_location": 1}, ("sample_points", "nanjing_admin"), True,
        _variants(
            "提取上传 POI 图层中落在上传南京行政区面的要素。",
            "擷取上傳 POI 圖層中落在上傳南京行政區面的要素。",
            "Extract uploaded POIs that fall within the uploaded Nanjing administrative polygon.",
            "Extrae los POI subidos que caen dentro del polígono administrativo de Nankín subido.",
            "アップロード済み南京行政区ポリゴン内にあるPOIを抽出してください。",
            "Extrait les POI importés situés dans le polygone administratif de Nankin importé.",
        ),
    ),
    (
        "convex_hull", {"geo_code": 4, "convex_hull": 1}, (), True,
        _variants(
            "计算中山陵、夫子庙、新街口、玄武湖四个地点的外包凸包。",
            "計算中山陵、夫子廟、新街口、玄武湖四個地點的外包凸包。",
            "Compute the convex hull of Zhongshanling, Fuzimiao, Xinjiekou and Xuanwu Lake.",
            "Calcula el casco convexo de Zhongshanling, Fuzimiao, Xinjiekou y el lago Xuanwu.",
            "中山陵、夫子廟、新街口、玄武湖の外包凸包を計算してください。",
            "Calcule l'enveloppe convexe de Zhongshanling, Fuzimiao, Xinjiekou et du lac Xuanwu.",
        ),
    ),
    (
        "bounding_box", {"data_io_read": 1, "bounding_boxes": 1}, ("parcels",), True,
        _variants(
            "计算上传地块图层每个要素的外接矩形。",
            "計算上傳地塊圖層每個要素的外接矩形。",
            "Create bounding boxes for every feature in the uploaded parcel layer.",
            "Crea cajas envolventes para cada elemento de la capa de parcelas subida.",
            "アップロードした区画レイヤーの各要素に外接矩形を作成してください。",
            "Crée les boîtes englobantes de chaque entité de la couche de parcelles importée.",
        ),
    ),
    (
        "centroid", {"data_io_read": 1, "centroid_layer": 1}, ("parcels",), True,
        _variants(
            "为上传地块图层生成几何中心点图层。",
            "為上傳地塊圖層產生幾何中心點圖層。",
            "Generate a centroid point layer for the uploaded parcels.",
            "Genera una capa de centroides para las parcelas subidas.",
            "アップロードした区画の重心ポイントレイヤーを生成してください。",
            "Génère une couche de centroïdes pour les parcelles importées.",
        ),
    ),
    (
        "point_on_surface", {"data_io_read": 1, "point_on_surface": 1}, ("parcels",), True,
        _variants(
            "为上传多边形图层生成确保落在面内的代表点。",
            "為上傳多邊形圖層產生保證落在面內的代表點。",
            "Generate a point-on-surface for every uploaded polygon feature.",
            "Genera un punto en superficie para cada polígono subido.",
            "アップロードした各ポリゴンの内部にある代表点を生成してください。",
            "Génère un point sur la surface pour chaque polygone importé.",
        ),
    ),
    (
        "simplify", {"data_io_read": 1, "simplify_geometry": 1}, ("parcels",), True,
        _variants(
            "以 1 米容差简化上传地块边界几何。",
            "以一公尺容差簡化上傳地塊邊界幾何。",
            "Simplify the uploaded parcel boundaries with a 1 metre tolerance.",
            "Simplifica los límites de las parcelas subidas con una tolerancia de 1 metro.",
            "アップロードした区画境界を許容値1メートルで簡略化してください。",
            "Simplifie les limites des parcelles importées avec une tolérance de 1 mètre.",
        ),
    ),
    (
        "fix_geometries", {"data_io_read": 1, "fix_geometries": 1}, ("invalid_parcels",), True,
        _variants(
            "修复上传图层中的无效几何。",
            "修復上傳圖層中的無效幾何。",
            "Repair invalid geometries in the uploaded layer.",
            "Repara las geometrías inválidas de la capa subida.",
            "アップロードしたレイヤー内の無効なジオメトリを修復してください。",
            "Répare les géométries invalides de la couche importée.",
        ),
    ),
    (
        "check_validity", {"data_io_read": 1, "check_validity": 1}, ("invalid_parcels",), False,
        _variants(
            "检查上传图层的几何有效性并报告问题。",
            "檢查上傳圖層的幾何有效性並回報問題。",
            "Validate the geometries in the uploaded layer and report problems.",
            "Valida las geometrías de la capa subida e informa los problemas.",
            "アップロードしたレイヤーのジオメトリ有効性を検査して問題を報告してください。",
            "Vérifie la validité géométrique de la couche importée et signale les problèmes.",
        ),
    ),
    (
        "reproject", {"data_io_read": 1, "reproject_layer": 1}, ("sample_points",), True,
        _variants(
            "将上传图层从 GCJ02 重投影转换为 WGS84。",
            "將上傳圖層從 GCJ02 重投影轉換為 WGS84。",
            "Reproject the uploaded layer from GCJ02 to WGS84.",
            "Reproyecta la capa subida desde GCJ02 a WGS84.",
            "アップロードしたレイヤーをGCJ02からWGS84へ再投影してください。",
            "Reprojette la couche importée de GCJ02 vers WGS84.",
        ),
    ),
    (
        "attribute_filter", {"data_io_read": 1, "extract_by_attribute": 1}, ("sample_points",), True,
        _variants(
            "筛选上传图层中 class 等于 station 的要素。",
            "篩選上傳圖層中 class 等於 station 的要素。",
            "Filter the uploaded layer for features whose class equals station.",
            "Filtra la capa subida por elementos cuyo class sea station.",
            "アップロードしたレイヤーからclassがstationの要素を抽出してください。",
            "Filtre la couche importée pour les entités dont class vaut station.",
        ),
    ),
    (
        "field_calculator", {"data_io_read": 1, "field_calculator": 1}, ("parcels",), True,
        _variants(
            "为上传地块添加面积字段 area_km2，单位平方公里。",
            "為上傳地塊新增面積欄位 area_km2，單位平方公里。",
            "Add an area_km2 field in square kilometres to the uploaded parcels.",
            "Añade a las parcelas subidas un campo area_km2 en kilómetros cuadrados.",
            "アップロードした区画に平方キロメートル単位のarea_km2フィールドを追加してください。",
            "Ajoute aux parcelles importées un champ area_km2 en kilomètres carrés.",
        ),
    ),
    (
        "slope", {"data_io_read": 1, "slope": 1}, ("dem",), True,
        _variants(
            "计算上传 DEM 的坡度栅格并显示。",
            "計算上傳 DEM 的坡度柵格並顯示。",
            "Compute and display the slope raster from the uploaded DEM.",
            "Calcula y muestra la pendiente del DEM subido.",
            "アップロードしたDEMから傾斜ラスターを計算して表示してください。",
            "Calcule et affiche la pente raster du DEM importé.",
        ),
    ),
    (
        "aspect", {"data_io_read": 1, "aspect": 1}, ("dem",), True,
        _variants(
            "计算上传 DEM 的坡向栅格并显示。",
            "計算上傳 DEM 的坡向柵格並顯示。",
            "Compute and display the aspect raster from the uploaded DEM.",
            "Calcula y muestra la orientación de ladera del DEM subido.",
            "アップロードしたDEMから傾斜方位ラスターを計算して表示してください。",
            "Calcule et affiche l'exposition raster du DEM importé.",
        ),
    ),
    (
        "hillshade", {"data_io_read": 1, "hillshade": 1}, ("dem",), True,
        _variants(
            "以方位角 315 度和高度角 45 度计算上传 DEM 的山体阴影。",
            "以方位角315度和高度角45度計算上傳 DEM 的山體陰影。",
            "Create hillshade from the uploaded DEM with azimuth 315 and altitude 45.",
            "Crea un sombreado del DEM subido con acimut 315 y altitud 45.",
            "方位角315度・高度角45度でアップロードDEMの陰影起伏を作成してください。",
            "Crée un ombrage du DEM importé avec un azimut de 315 et une altitude de 45.",
        ),
    ),
    (
        "zonal_statistics", {"data_io_read": 2, "zonal_statistics": 1}, ("dem", "nanjing_admin"), True,
        _variants(
            "按上传行政区面分区统计上传 DEM 的平均海拔。",
            "依上傳行政區面分區統計上傳 DEM 的平均海拔。",
            "Calculate mean elevation of the uploaded DEM by uploaded administrative zones.",
            "Calcula la elevación media del DEM subido por las zonas administrativas subidas.",
            "アップロード行政区ポリゴンごとにDEMの平均標高をゾーン統計してください。",
            "Calcule l'altitude moyenne du DEM importé par zones administratives importées.",
        ),
    ),
    (
        "reclassify", {"data_io_read": 1, "slope": 1, "reclassify_raster": 1}, ("dem",), True,
        _variants(
            "将上传 DEM 坡度重分类为 0-15、15-30、超过30度三档。",
            "將上傳 DEM 坡度重分類為0-15、15-30、超過30度三檔。",
            "Reclassify uploaded DEM slope into 0-15, 15-30 and above-30 degree classes.",
            "Reclasifica la pendiente del DEM subido en clases 0-15, 15-30 y más de 30 grados.",
            "アップロードDEMの傾斜を0-15、15-30、30度超の3区分に再分類してください。",
            "Reclasse la pente du DEM importé en classes 0-15, 15-30 et plus de 30 degrés.",
        ),
    ),
    (
        "export_chain", {"data_io_read": 1, "fix_geometries": 1, "reproject_layer": 1, "buffer": 1, "dissolve_layer": 1, "export_result": 1}, ("parcels",), False,
        _variants(
            "修复上传地块几何，重投影到 EPSG:4548，做500米缓冲并融合后导出 GeoJSON。",
            "修復上傳地塊幾何，重投影到 EPSG:4548，做500公尺緩衝並融合後匯出 GeoJSON。",
            "Fix uploaded parcel geometries, reproject to EPSG:4548, buffer 500 m, dissolve and export GeoJSON.",
            "Repara las geometrías de parcelas subidas, reproyecta a EPSG:4548, aplica buffer de 500 m, disuelve y exporta GeoJSON.",
            "アップロード区画を修復しEPSG:4548へ再投影、500mバッファ、ディゾルブ後にGeoJSONで出力してください。",
            "Répare les parcelles importées, reprojette en EPSG:4548, applique un tampon de 500 m, dissous et exporte en GeoJSON.",
        ),
    ),
    (
        "multiturn_compare", {"geo_code": 1, "query_poi": 1}, (), True,
        _variants(
            "再查南京新街口 500 米内的茶百道，并与上一轮蜜雪冰城比较密度。",
            "再查南京新街口五百公尺內的茶百道，並與上一輪 Mixue 比較密度。",
            "Now find Chabaidao within 500 metres of Xinjiekou and compare its density with the previous Mixue result.",
            "Ahora busca Chabaidao a 500 metros de Xinjiekou y compara su densidad con el resultado anterior de Mixue.",
            "次に新街口500m圏のChabaidaoを調べ、前回のMixue結果と密度を比較してください。",
            "Cherche maintenant Chabaidao dans un rayon de 500 m de Xinjiekou et compare sa densité au résultat Mixue précédent.",
        ),
    ),
)


def _family_cases() -> list[PromptCase]:
    cases: list[PromptCase] = []
    for intent, tools, uploads, expect_map, variants in FAMILIES:
        for language in LANGUAGES:
            session_group = None
            if intent in {"poi_query", "multiturn_compare"}:
                session_group = f"density-{language}"
            cases.append(PromptCase(
                id=f"M{len(cases) + 1:03d}",
                intent=intent,
                language=language,
                prompt=variants[language],
                expected_tools=dict(tools),
                upload_names=tuple(uploads),
                expect_map=expect_map,
                session_group=session_group,
            ))
    return cases


BOUNDARY_CASES = (
    PromptCase("M193", "transform_outside_china", "zh", "判断旧金山坐标 -122.4194,37.7749 是否在中国坐标偏转范围外。", {"geo_transform": 1}, boundary=True),
    PromptCase("M194", "buffer_one_metre", "en", "Create a 1 metre buffer around every feature in the uploaded point layer.", {"data_io_read": 1, "buffer": 1}, ("sample_points",), True, True),
    PromptCase("M195", "overlay_difference", "zh_tw", "計算兩個上傳圖層的 difference 差集。", {"data_io_read": 2, "overlay": 1}, ("xuanwuhu", "zijinshan"), True, True),
    PromptCase("M196", "nearest_zero_distance", "es", "Une los POI subidos con paradas de autobús usando una distancia máxima de 0 metros.", {"data_io_read": 2, "join_by_nearest": 1}, ("sample_points", "bus_stations"), True, True),
    PromptCase("M197", "attribute_not_equal", "ja", "アップロードしたレイヤーからclassがstationではない要素を抽出してください。", {"data_io_read": 1, "extract_by_attribute": 1}, ("sample_points",), True, True),
    PromptCase("M198", "simplify_zero", "fr", "Simplifie la couche de parcelles importée avec une tolérance de 0 mètre.", {"data_io_read": 1, "simplify_geometry": 1}, ("parcels",), True, True),
    PromptCase("M199", "reclass_exact_edges", "zh", "将上传 DEM 的坡度按临界值 15 度和30度重分类。", {"data_io_read": 1, "slope": 1, "reclassify_raster": 1}, ("dem",), True, True),
    PromptCase("M200", "validity_invalid", "en", "Check validity of the deliberately invalid uploaded polygon layer without modifying it.", {"data_io_read": 1, "check_validity": 1}, ("invalid_parcels",), False, True),
)


CASES = tuple(_family_cases()) + BOUNDARY_CASES

assert len(FAMILIES) == 32
assert len(CASES) == 200
assert {case.language for case in CASES} == set(LANGUAGES)
