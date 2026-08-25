---
name: spatial_join
description: "空间连接最佳实践"
requires_toolkits: [vector_analysis]
workspace_attention: [crs, geometry_type, feature_count]
risk_awareness: [crs_mismatch, geometry_type_mismatch]
strategy_guidance:
  - "空间连接前检查两个图层 CRS 是否一致"
  - "检查几何类型匹配（点对点、面对点等）"
max_chars: 400
---

# 空间连接

## 关键规则
- 确保两个图层 CRS 一致
- 注意一对多连接可能导致要素数量膨胀
