import type { ArtifactSummary } from '@/types/runtime';

const rasterExamples = [
  { title: '按边界裁剪栅格', prompt: '用当前选中的矢量面边界掩膜裁剪栅格，交付裁剪后的原生 GeoTIFF。' },
  { title: '按面分区统计', prompt: '用当前选中的矢量面作分区，统计栅格每个分区的平均值，交付分区统计表。' },
];

const vectorExamples = [
  { title: '300 米缓冲区', prompt: '对当前选中的矢量图层逐要素生成 300 米缓冲区，保留每个要素的属性，交付 GeoJSON。' },
];

export function runtimeWorkflowExamples(inputs: ArtifactSummary[]) {
  const unique = [...new Map(inputs.map(artifact => [artifact.artifact_id, artifact])).values()];
  const spatial = unique.map(artifact => {
    const inspection = artifact.schema?.inspection as Record<string, unknown> | undefined;
    return {
      kind: artifact.type === 'File' ? inspection?.artifact_type : artifact.type,
      crs: artifact.crs,
      geometry: inspection?.geometry_types ?? artifact.schema?.geometry_types,
    };
  });

  if (spatial.length === 1 && spatial[0].kind === 'Vector' && spatial[0].crs) return vectorExamples;
  if (spatial.length !== 2 || spatial.some(item => !item.crs)) return [];

  const boundary = spatial.find(item => item.kind === 'Vector');
  const types = boundary?.geometry;
  const polygon = Array.isArray(types) && types.length > 0 && types.every(type => type === 'Polygon' || type === 'MultiPolygon');
  return polygon && spatial.some(item => item.kind === 'Raster') ? rasterExamples : [];
}
