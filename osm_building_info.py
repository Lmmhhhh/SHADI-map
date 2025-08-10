import osmnx as ox
import geopandas as gpd
import pandas as pd

# 중심 좌표 (충남대)
latitude = 36.3659
longitude = 127.3455
tags = {"building": True}

# OSM에서 건물 정보 가져오기
gdf = ox.features.features_from_point((latitude, longitude), tags=tags, dist=500)
buildings = gdf[gdf['geometry'].geom_type.isin(['Polygon', 'MultiPolygon'])].copy()

# 총 건물 수
total = len(buildings)

# 높이 태그 개수
has_height = buildings['height'].notna().sum() if 'height' in buildings.columns else 0
has_levels = buildings['building:levels'].notna().sum() if 'building:levels' in buildings.columns else 0

# 요약 출력
print(f"전체 건물 수: {total}")
print(f" - 'height' 태그 있는 건물 수: {has_height}")
print(f" - 'building:levels' 태그 있는 건물 수: {has_levels}")
print(f" - 아무 높이 정보도 없는 건물 수: {total - (has_height + has_levels)}\n")

# 층수 파싱 함수
def parse_levels(level_str):
    try:
        levels = max([int(float(x)) for x in str(level_str).split(';')])
        return levels
    except:
        return None

# 층수 있는 건물만 추출
buildings_with_levels = buildings[buildings['building:levels'].notna()].copy()
buildings_with_levels['levels'] = buildings_with_levels['building:levels'].apply(parse_levels)
buildings_with_levels['estimated_height'] = buildings_with_levels['levels'] * 3.3  # 평균 층고 적용

# 보기 좋게 정렬해서 출력 (컬럼 너비 맞춤)
with pd.option_context('display.max_rows', None,
                       'display.max_columns', None,
                       'display.width', 120,
                       'display.colheader_justify', 'center',
                       'display.unicode.east_asian_width', True):
    print("🏢 높이 정보(building:levels)가 있는 건물 목록:\n")
    print(buildings_with_levels[['building', 'name', 'building:levels', 'levels', 'estimated_height']].to_string(index=True))

print(buildings.columns.tolist())


