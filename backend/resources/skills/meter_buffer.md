---
name: meter_buffer
description: "米制缓冲区最佳实践"
requires_toolkits: [vector_analysis]
workspace_attention: [input_crs, geometry_type]
risk_awareness: [geographic_crs_metric_buffer]
strategy_guidance:
  - "米制缓冲前必须先确认输入图层 CRS"
  - "如 CRS 是 EPSG:4326，先 reproject 到本地 UTM 带"
max_chars: 500
---

# 米制缓冲区

## 适用场景
用户要求以米或千米为单位对图层做缓冲区。

## 关键规则
- 不要在 EPSG:4326（地理坐标系/经纬度）上直接做米制缓冲
- 缓冲前必须确保图层使用投影坐标系（单位是米）

## 反模式
- ❌ `buffer(geom, 500)` —— 如果 geom 是 EPSG:4326，500 的单位是度
- ✅ 先重投影到 EPSG:4548/UTM，再 `buffer(geom, 500)`
