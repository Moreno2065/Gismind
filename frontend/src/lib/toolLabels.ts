// GIS 工具名的中英文标签映射，供 ThinkingCollapse 等展示

const TOOL_LABELS: Record<string, string> = {
  // 地理编码 / POI
  geo_code: '地理编码',
  query_poi: 'POI 查询',
  geo_transform: '坐标转换',

  // 矢量分析
  buffer_layer: '缓冲区',
  buffer: '缓冲区',
  overlay: '叠加分析',
  intersect_layer: '相交',
  difference_layer: '差集',
  union_layer: '并集',
  symmetrical_difference: '对称差',
  clip_layer: '裁剪',
  extract_by_location: '按位置筛选',
  dissolve_layer: '融合',
  merge_layers: '合并图层',
  join_by_location: '空间连接',
  join_by_nearest: '最近邻连接',
  count_points_in_polygon: '面内点计数',
  voronoi: '泰森多边形',
  isochrone: '等时圈',

  // 几何变换
  reproject_layer: '重投影',
  centroid_layer: '质心',
  point_on_surface: '面内点',
  simplify_geometry: '简化几何',
  fix_geometries: '修复几何',
  check_validity: '几何检查',
  multipart_to_singlepart: '拆分部件',
  delete_duplicate_geometries: '去重',
  snap_geometries: '几何吸附',
  convex_hull: '凸包',
  bounding_boxes: '外包矩形',
  batch_reproject_layers: '批量重投影',

  // 属性操作
  extract_by_attribute: '属性筛选',
  keep_fields: '保留字段',
  rename_field: '重命名字段',
  field_calculator: '字段计算',

  // 栅格分析
  reproject_raster: '栅格重投影',
  clip_raster_by_mask: '栅格按掩膜裁剪',
  clip_raster_by_extent: '栅格按范围裁剪',
  raster_calculator: '栅格计算',
  zonal_statistics: '分区统计',
  raster_sampling: '栅格采样',
  rasterize_vector: '矢转栅',
  polygonize_raster: '栅转矢',
  slope: '坡度',
  aspect: '坡向',
  hillshade: '山体阴影',
  contour: '等高线',
  reclassify_raster: '栅格重分类',
  terrain_ruggedness_index: '崎岖指数',
  topographic_position_index: '位置指数',
  roughness: '粗糙度',

  // 数据 IO
  load_vector: '加载矢量',
  load_raster: '加载栅格',
  load_csv: '加载CSV',
  csv_to_points: 'CSV转点',
  summarize_layer: '图层摘要',
  export_result: '导出结果',
  data_io_read: '数据读取',
  map_layer_build: '图层构建',

  // Kernel
  select_toolkit: '选择工具集',
  inspect_workspace: '检查工作区',
  suggest_skill: '推荐技能',
  load_skill: '加载技能',
  proactive_clarification: '主动澄清',

  // 沙箱
  parse_zip: '解析Zip',
  code_executor: '代码执行',
  fetch_from_redis: '从Redis取',
};

/** 返回工具名的中文标签，未映射则返回原名。*/
export function getToolLabel(name: string | undefined): string {
  if (!name) return '';
  return TOOL_LABELS[name] ?? name;
}
