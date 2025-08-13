import time, math, datetime, pytz, re, warnings
import pandas as pd, geopandas as gpd, folium, osmnx as ox
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.affinity import translate
from shapely.validation import make_valid
from shapely.errors import GEOSException
from shapely.ops import unary_union, transform
from pysolar.solar import get_altitude, get_azimuth
from pyproj import Transformer
import numpy as np
from shapely.affinity import scale, rotate

# ────────────────────────────── 기본 설정 ──────────────────────────────
tz   = pytz.timezone("Asia/Seoul")
now  = tz.localize(datetime.datetime(2024, 7, 31, 15, 0, 0))   # 분석 시각
WIDTH_RATIO_TREE = 7                                         # 나무 그림자 폭 = 높이×1.5
proj = Transformer.from_crs(4326, 5179, always_xy=True)        # 면적(m²) 계산용
SHELTER_SCALE = 3                                            # 쉼터 그림자 폭

warnings.filterwarnings("ignore", message="I don't know about leap seconds")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="shapely")

# ────────────────────────────── 유틸 함수 ──────────────────────────────
def shadow_len(h, alt):                        # 그림자 길이(m)
    return 0 if alt <= 0 else h / math.tan(math.radians(alt))

def offset_latlon(lat, lon, dist_m, brg):
    dlat =  dist_m*math.cos(math.radians(brg)) / 111_320
    dlon = (dist_m*math.sin(math.radians(brg))
            / (40075_000*math.cos(math.radians(lat))/360))
    return lat+dlat, lon+dlon

def tree_shadow_polygon(lat, lon, h, alt, azi):
    L = shadow_len(h, alt)
    half = max(1, h*WIDTH_RATIO_TREE)/2
    p1 = offset_latlon(lat, lon, half, (azi+90)%360)
    p2 = offset_latlon(lat, lon, half, (azi-90)%360)
    end_lat, end_lon = offset_latlon(lat, lon, L, azi)
    p3 = offset_latlon(end_lat, end_lon, half, (azi-90)%360)
    p4 = offset_latlon(end_lat, end_lon, half, (azi+90)%360)
    return Polygon([p1, p2, p3, p4])

def tree_shadow_ellipse(lat, lon, r_m, alt, azi):
    """수관 반경 r_m → 타원형 그림자 Polygon 반환"""
    if alt <= 0:
        return Polygon()            # 밤이면 그림자 X

    # ─ 1) 좌표체계: WGS84 → EPSG:5179 ─
    to5179 = Transformer.from_crs(4326, 5179, always_xy=True)
    to4326 = Transformer.from_crs(5179, 4326, always_xy=True)
    x0, y0 = to5179.transform(lon, lat)

    circle = Point(x0, y0).buffer(r_m)       # 반경 r_m짜리 원

    # ─ 2) 원 → 타원(늘리기) ─
    stretch = 1 / math.tan(math.radians(alt))     # 고도 낮을수록 길어짐
    ellip   = scale(circle, 1, stretch, origin=(x0, y0))

    # ─ 3) 태양과 직각이 되도록 회전 =-0987654321
    ellip   = rotate(ellip, (azi + 90) % 360, origin=(x0, y0))

    # ─ 4) 트렁크와 타원 앞머리가 맞게 L/2 만큼 뒤로 이동 ─
    L   = shadow_len(r_m, alt)                   # 캐노피 기준 그림자 길이
    dx  =  (L/2) * math.sin(math.radians(azi))
    dy  =  (L/2) * math.cos(math.radians(azi))
    ellip = translate(ellip, xoff=dx, yoff=dy)

    # ─ 5) EPSG:4326 로 환원 ─
    shadow = transform(lambda x, y, z=None: to4326.transform(x, y), ellip)
    return make_valid(shadow)

def shelter_shadow_octagon(lat, lon, diameter_m, height_m, alt, azi):
    if alt <= 0:
        return Polygon()

    # 반지름
    r = diameter_m / 2
    # 1) 원점(0,0)에 반지름 r짜리 팔각형 생성
    angles = [math.radians(22.5 + 45*i) for i in range(8)]
    base_pts = [(r*math.cos(th), r*math.sin(th)) for th in angles]
    base = Polygon(base_pts)

    # 2) 그림자 길이 (height_m 기준)
    L = shadow_len(height_m, alt)
    # 3) 늘리기 배율 = L / r
    stretch = L / r
    shadow = scale(base, 1, stretch, origin=(0, 0))

    # 4) 태양과 직각으로 회전
    shadow = rotate(shadow, (azi + 90) % 360, origin=(0, 0))

    # 5) 기둥(쉼터)과 그림자 이어붙이기 (반만 평행이동)
    dx, dy = (L/2)*math.sin(math.radians(azi)), (L/2)*math.cos(math.radians(azi))
    shadow = translate(shadow, xoff=dx, yoff=dy)

    # 6) WGS84 좌표로 이동
    to5179 = Transformer.from_crs(4326, 5179, always_xy=True)
    to4326 = Transformer.from_crs(5179, 4326, always_xy=True)
    cx, cy = to5179.transform(lon, lat)
    shadow = translate(shadow, xoff=cx, yoff=cy)
    return make_valid(transform(lambda x, y, z=None: to4326.transform(x, y), shadow))


# ────────── 0. 충남대 50 m 버퍼 ──────────
CENTER_CNU = (36.36917, 127.34515)          # 충남대 정문 좌표 (대략)
DIST_M     = 1000                            
deg = DIST_M / 111_320                      # 위도 1° ≈ 111,320 m
buffer_50m_poly = Polygon([
    (CENTER_CNU[1]-deg, CENTER_CNU[0]-deg),
    (CENTER_CNU[1]+deg, CENTER_CNU[0]-deg),
    (CENTER_CNU[1]+deg, CENTER_CNU[0]+deg),
    (CENTER_CNU[1]-deg, CENTER_CNU[0]+deg)
])

CENTER = CENTER_CNU                         # folium 지도 중심

WIDTH_RATIO_TREE = 3.0                 

# ────────── building_shadow_polygon 재정의 ──────────
def building_shadow_polygon(poly, h, alt, azi):
    if alt <= 0:
        return Polygon()

    L  = shadow_len(h, alt)
    dy =  L*math.cos(math.radians(azi)) / 111_320
    dx = (L*math.sin(math.radians(azi))
          / (40075_000*math.cos(math.radians(poly.centroid.y))/360))

    def _shadow(p):
        src  = list(p.exterior.coords)
        dest = [(x+dx, y+dy) for x, y in src]
        quads = [Polygon([src[i], src[i+1], dest[i+1], dest[i]])
                 for i in range(len(src)-1)]
        return unary_union([Polygon(dest), *quads])

    # 1) 그림자 다각형 생성
    if isinstance(poly, Polygon):
        shadow = _shadow(poly)
    else:  # MultiPolygon
        shadow = unary_union([_shadow(g) for g in poly.geoms])

    # 2) 원래 건물 footprint 부분 제거 → 순수 그림자만 남김
    shadow = shadow.difference(poly)

    return make_valid(shadow)


def geom_area_m2(geom):               
    proj_fn = lambda x, y, z=None: proj.transform(x, y)
    return transform(proj_fn, geom).area

def to_float_or_none(val):
    """문자열에서 숫자·소수점만 남겨 float 변환, 없으면 None 반환"""
    num = re.sub(r"[^0-9.]", "", str(val))
    return float(num) if num else None


# ────────────────────────────── 시작 로그 ──────────────────────────────
t0 = time.time()
print("▶ [START] shadow_map_debug.py 실행")

# ───────────────────── 0. 유성구 행정경계 폴리곤 ──────────────────────
admin_gdf  = ox.geocode_to_gdf("Yuseong-gu, Daejeon, South Korea")
admin_poly = make_valid(admin_gdf.loc[0, "geometry"].buffer(0))
CENTER     = (admin_poly.centroid.y, admin_poly.centroid.x)

# ───────────────────── 1. 가로수 CSV → 그림자 ────────────────────────
print("  • 가로수 CSV 로드 중 …")
trees = pd.read_csv("data/대전광역시_가로수 현황_20221201.csv", encoding="euc-kr")
trees_gdf = gpd.GeoDataFrame(
    trees.dropna(subset=["경도","위도"]),
    geometry=[Point(xy) for xy in zip(trees["경도"], trees["위도"])],
    crs="EPSG:4326",
)
trees_gdf = gpd.clip(trees_gdf, buffer_50m_poly)
print(f"    → 가로수 {len(trees_gdf):,} 개 (유성구)")

tree_layers = []
for _, r in trees_gdf.iterrows():
    lat, lon  = r["위도"], r["경도"]
    h         = float(r.get("수고", 4))          # 수고 없으면 4 m
    crown_r   = h * 0.25                        # 수관 반경 ≈ 높이 1/4
    # ⬇⬇⬇ azimuth 180도 반전
    alt, azi  = get_altitude(lat, lon, now), (get_azimuth(lat, lon, now) + 180) % 360

    poly = tree_shadow_ellipse(lat, lon, crown_r, alt, azi)  
    if poly.is_empty: continue

    area  = geom_area_m2(poly)
    tip   = f"Tree shadow<br>H={h:.1f} m / crown≈{crown_r:.1f} m<br>{area:,.1f} ㎡"
    tree_layers.append((poly, tip))
print(f"    → 그림자 폴리곤 {len(tree_layers):,} 개 생성")

# ───────────────────── 1-B. 그늘막 쉼터 CSV → 그림자 ─────────────────────
print("  • 그늘막 쉼터 CSV 로드 중 …")
shel = pd.read_csv("data/대전광역시 유성구_그늘막쉼터_20240920.csv", encoding="euc-kr")

shel_gdf = gpd.GeoDataFrame(
    shel.dropna(subset=["위도", "경도"]),
    geometry=[Point(xy) for xy in zip(shel["위도"], shel["경도"])],
    crs="EPSG:4326"
)
shel_gdf = gpd.clip(shel_gdf, buffer_50m_poly)   # 버퍼 범위로 자르기
print(f"    → 쉼터 {len(shel_gdf):,} 개 (버퍼 범위)")


# ─────────────────── 2-A. Shapefile 건물 → 보라색 그림자 ───────────────────
print("  • Shapefile 건물 로드 중 …")
shp_gdf = (gpd.read_file("data/CH_D010_00_20250731.shp", encoding="euc-kr")
             .to_crs(epsg=4326))
shp_gdf = shp_gdf[shp_gdf["A4"].str.contains("대전광역시", na=False)]
shp_gdf = gpd.clip(shp_gdf, buffer_50m_poly)

shp_layers = []
for _, r in shp_gdf.iterrows():
    poly = make_valid(r.geometry)
    if poly.is_empty: continue
    floors = pd.to_numeric(r.get("A25"), errors="coerce")
    h      = floors*3 if not pd.isna(floors) else 10.0
    # ⬇⬇⬇ azimuth 180도 반전
    alt, azi = get_altitude(poly.centroid.y, poly.centroid.x, now), (get_azimuth(poly.centroid.y, poly.centroid.x, now) + 180) % 360
    s_poly  = building_shadow_polygon(poly, h, alt, azi)
    if not s_poly.is_valid or s_poly.is_empty: continue
    shp_layers.append((s_poly, f"Shapefile<br>높이≈{h:.1f} m"))

print(f"    → Shapefile 그림자 {len(shp_layers):,} 개")

# 셰이프 건물 합집합(중복 제거용)
shp_union = unary_union([g for g, _ in shp_layers])

shel_layers = []
for _, r in shel_gdf.iterrows():
    lat, lon = r["위도"], r["경도"]

    # CSV에 있는 실제 값을 읽어와서 사용
    shelter_h = to_float_or_none(r.get("전체높이"))    # 전체높이(m)
    canopy_d  = to_float_or_none(r.get("펼침지름"))     # 펼침지름(m)
    if shelter_h is None: shelter_h = 3.0
    if canopy_d  is None: canopy_d  = 3.0

    # ⬇⬇⬇ azimuth 180도 반전
    alt, azi = get_altitude(lat, lon, now), (get_azimuth(lat, lon, now) + 180) % 360
    # diameter_m=canopy_d, height_m=shelter_h 순으로 인자 전달
    poly = shelter_shadow_octagon(lat, lon, canopy_d, shelter_h, alt, azi)

    if poly.is_empty: continue

    area = geom_area_m2(poly)
    tip  = (f"쉼터 팔각 그림자<br>지름≈{canopy_d:.1f} m<br>{area:,.1f} ㎡")
    shel_layers.append((poly, tip))
    print(f"    → 쉼터 그림자 폴리곤 {len(tree_layers):,} 개 생성")

# ─────────────────── 2-B. OSM 건물 → 빨간색 그림자 ───────────────────
print("  • OSM 건물 로드 중 …")
try:
    osm = ox.features_from_polygon(buffer_50m_poly, tags={"building": True})
except ox._errors.InsufficientResponseError:
    osm = ox.features_from_point(CENTER_CNU, dist=DIST_M, tags={"building": True})
osm = osm.to_crs(epsg=4326)
print(f"    → OSM 건물 {len(osm):,} 개")

osm_layers = []
for _, row in osm.iterrows():
    poly = make_valid(row.geometry)
    if poly.is_empty or poly.intersects(shp_union):     # Shapefile과 겹치면 skip
        continue
    else:                                # OSM 태그 → ③ 기본값
        # height 태그가 있으면 그대로 float, 없으면 None
        h_height = to_float_or_none(row.get("height"))
        # building:levels 가 "5;4" 같이 여러 개일 때 최대값만 골라 3m/층 으로 환산
        raw_lv = row.get("building:levels")
        lv_list = [int(x) for x in re.findall(r'\d+', str(raw_lv) if raw_lv else "")]
        if lv_list:
            h_levels = max(lv_list) * 3
        else:
            h_levels = None

        # 후보들 중 존재하는 값만 골라 최대값 → 없으면 10 m
        candidates = [h for h in (h_height, h_levels) if h is not None]
        h = max(candidates) if candidates else 10.0

    # ⬇⬇⬇ azimuth 180도 반전
    alt, azi = get_altitude(poly.centroid.y, poly.centroid.x, now), (get_azimuth(poly.centroid.y, poly.centroid.x, now) + 180) % 360
    
    if (poly.is_empty or poly.intersects(shp_union) or
        not isinstance(poly, (Polygon, MultiPolygon))):
        continue

    s_poly = building_shadow_polygon(poly, h, alt, azi)
    if s_poly.is_empty or not s_poly.is_valid:
        continue
    tooltip = f"OSM<br>높이≈{h:.1f} m"
    osm_layers.append((s_poly, tooltip))
    
print(f"    → OSM 그림자 {len(osm_layers):,} 개")


# ───────────────────── 3. Folium 시각화  ─────────────────────
print("  • Folium 지도 생성 중 …")

m = folium.Map(location=CENTER, zoom_start=15, tiles=None)
folium.TileLayer("OpenStreetMap", name="Default").add_to(m)
folium.TileLayer("CartoDB positron", name="Light").add_to(m)

# 3-A. 건물 그림자 레이어 (보라)
bld_fg = folium.FeatureGroup(name="🏢 건물 그림자", show=False)
for poly, tip in shp_layers + osm_layers:
    folium.GeoJson(
        poly.__geo_interface__,
        style_function=lambda x: {
            "fillColor": "#28252c",
            "color": "#463f4f",
            "weight": 0.5,
            "fillOpacity": 0.5
        },
        tooltip=tip
    ).add_to(bld_fg)
m.add_child(bld_fg)

# 3-B. 나무 그림자 레이어 (연녹색)
tree_fg = folium.FeatureGroup(name="🌳 나무 그림자", show=True)
for _, r in trees_gdf.iterrows():
    lat, lon = r["위도"], r["경도"]
    # 고정 높이 10m, 수관폭 6m 적용
    # ⬇⬇⬇ azimuth 180도 반전
    alt, azi = get_altitude(lat, lon, now), (get_azimuth(lat, lon, now) + 180) % 360
    poly = tree_shadow_ellipse(lat, lon, 6/2, alt, azi)
    if poly.is_empty: continue
    folium.GeoJson(
        poly.__geo_interface__,
        style_function=lambda _: {
            "fillColor": "#7fc97f",
            "color": "#4daf4a",
            "weight": 0.3,
            "fillOpacity": 0.6
        }
    ).add_to(tree_fg)
m.add_child(tree_fg)

# 3-C. 쉼터 그림자 레이어 (주황)
shelter_fg = folium.FeatureGroup(name="⛱️ 쉼터 그림자", show=True)
for _, r in shel_gdf.iterrows():
    lon = r.geometry.x
    lat = r.geometry.y

    shelter_h = to_float_or_none(r.get("전체높이")) or 2.5
    canopy_d  = to_float_or_none(r.get("펼침지름")) or 2.0

    # ⬇⬇⬇ azimuth 180도 반전
    alt = get_altitude(lat, lon, now)
    azi = (get_azimuth(lat, lon, now) + 180) % 360
    poly = shelter_shadow_octagon(lat, lon, canopy_d, shelter_h, alt, azi)
    if poly.is_empty:
        continue

    folium.GeoJson(
        poly.__geo_interface__,
        style_function=lambda _: {
            "fillColor": "#fdae61",
            "color": "#e66101",
            "weight": 0.3,
            "fillOpacity": 0.6
        },
        tooltip=f"쉼터 그림자<br>높이≈{shelter_h} m / 지름≈{canopy_d} m"
    ).add_to(shelter_fg)


# ───────────────────── 4. PostGIS 저장 (추가) ─────────────────────
from sqlalchemy import create_engine, text

# 4-0) Postgres 접속 정보 (환경에 맞게 수정)
PG_URL = "postgresql://postgres:804009@localhost:5432/shadi"
engine = create_engine(PG_URL)

# 4-1) GeoDataFrame 준비
#  - 각 레이어에서 geometry만 뽑아 테이블로 저장
gdf_building = gpd.GeoDataFrame(
    geometry=[g for g, _ in (shp_layers + osm_layers)],
    crs="EPSG:4326"
)
gdf_tree = gpd.GeoDataFrame(
    geometry=[g for g, _ in tree_layers],
    crs="EPSG:4326"
)
gdf_shelter = gpd.GeoDataFrame(
    geometry=[g for g, _ in shel_layers],
    crs="EPSG:4326"
)

# 4-2) PostGIS로 저장 (없으면 생성, 있으면 교체)
gdf_building.to_postgis("shadow_building_20240731_1500", engine, if_exists="replace", index=False)
gdf_tree.to_postgis("shadow_tree_20240731_1500", engine, if_exists="replace", index=False)
gdf_shelter.to_postgis("shadow_shelter_20240731_1500", engine, if_exists="replace", index=False)

# 4-3) 공간 인덱스 생성
with engine.begin() as conn:
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS shadow_building_20240731_1500_gix
          ON shadow_building_20240731_1500 USING GIST (geometry);
        CREATE INDEX IF NOT EXISTS shadow_tree_20240731_1500_gix
          ON shadow_tree_20240731_1500 USING GIST (geometry);
        CREATE INDEX IF NOT EXISTS shadow_shelter_20240731_1500_gix
          ON shadow_shelter_20240731_1500 USING GIST (geometry);
    """))

# 4-4) UNION 테이블 생성 (건물+나무+쉼터)
with engine.begin() as conn:
    conn.execute(text("DROP TABLE IF EXISTS shadow_union_20240731_1500;"))
    conn.execute(text("""
        CREATE TABLE shadow_union_20240731_1500 AS
        SELECT ST_UnaryUnion(geometry) AS geometry
        FROM (
          SELECT geometry FROM shadow_building_20240731_1500
          UNION ALL
          SELECT geometry FROM shadow_tree_20240731_1500
          UNION ALL
          SELECT geometry FROM shadow_shelter_20240731_1500
        ) s;
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS shadow_union_20240731_1500_gix
          ON shadow_union_20240731_1500 USING GIST (geometry);
    """))

print("PostGIS 저장 및 UNION 테이블 생성 완료")

# ───────────────────── 5. 경로 산출(최단/시원길) + 지도 레이어 추가 ─────────────────────
import psycopg2
from psycopg2.extras import RealDictCursor

# (1) 출발/도착 좌표 설정 (lon, lat)  ← 필요에 맞게 바꾸세요
SRC = (127.346442, 36.365188)   # 예시: 공릉천 인근
DST = (127.343051, 36.368850)   # 예시: 유성온천역 쪽

# (2) ‘시원길’ 가중치: 그늘 비율이 높을수록 비용을 더 낮게(선호) 만드는 계수
COOL_WEIGHT = 0.8   # 0.0~1.0 권장. 1.0이면 그늘 100% 구간 비용이 거의 0에 가까워짐

# (3) 경로 계산용 SQL (해당 시각: 2024-07-31 15:00 → union 테이블: shadow_union_20240731_1500)
#     - len_m: 실제 edge 길이(m)
#     - shade_ratio: edge의 그늘 비율(0~1), union 폴리곤과의 교차 길이 / 전체 길이
#     - cool_cost: len_m * (1 - COOL_WEIGHT * shade_ratio)  → 그늘 많을수록 더 작은 비용
SQL_ROUTE = f"""
WITH
u AS (
  SELECT geometry FROM shadow_union_20240731_1500 LIMIT 1
),
edges AS (
  SELECT
    w.id, w.source, w.target, w.geom,
    ST_Length(w.geom::geography) AS len_m,
    COALESCE(
      ST_Length(ST_Intersection(w.geom::geography, u.geometry::geography))
      / NULLIF(ST_Length(w.geom::geography), 0), 0
    ) AS shade_ratio
  FROM ways_raw w
  LEFT JOIN u ON ST_Intersects(w.geom, u.geometry)
),
src AS (
  SELECT id
  FROM ways_raw_vertices_pgr
  ORDER BY the_geom <-> ST_SetSRID(ST_Point(%s,%s), 4326)
  LIMIT 1
),
dst AS (
  SELECT id
  FROM ways_raw_vertices_pgr
  ORDER BY the_geom <-> ST_SetSRID(ST_Point(%s,%s), 4326)
  LIMIT 1
),
shortest AS (
  SELECT * FROM pgr_dijkstra(
    --[수정] 바깥 CTE(edges) 참조 금지 → 내부에서 길이 계산 Subquery로 직접 작성
    $q$
    SELECT id, source, target,
           len_m AS cost,
           len_m AS reverse_cost
    FROM (
      SELECT
        w.id, w.source, w.target, w.geom,
        ST_Length(w.geom::geography) AS len_m
      FROM ways_raw w
    ) AS in_edges
    $q$,
    (SELECT id FROM src), (SELECT id FROM dst),
    false
  )
),
coolest AS (
  SELECT * FROM pgr_dijkstra(
    -- [수정] 바깥 CTE(edges) 참조 금지 → 내부에서 shade_ratio까지 직접 계산
    $q$
    SELECT
      id, source, target,
      GREATEST(len_m * (1 - {COOL_WEIGHT} * shade_ratio), 0.1) AS cost,
      GREATEST(len_m * (1 - {COOL_WEIGHT} * shade_ratio), 0.1) AS reverse_cost
    FROM (
      SELECT
        w.id, w.source, w.target, w.geom,
        ST_Length(w.geom::geography) AS len_m,
        --  내부에서 바로 그늘비율 계산: union 테이블 직접 JOIN
        COALESCE(
          ST_Length(ST_Intersection(w.geom::geography, su.geometry::geography))
          / NULLIF(ST_Length(w.geom::geography), 0), 0
        ) AS shade_ratio
      FROM ways_raw w
      LEFT JOIN shadow_union_20240731_1500 su
        ON ST_Intersects(w.geom, su.geometry)
    ) AS in_edges
    $q$,
    (SELECT id FROM src), (SELECT id FROM dst),
    false
  )
),
shortest_path AS (
  SELECT
    ST_LineMerge(ST_Union(e.geom)) AS geom,
    SUM(e.len_m) AS total_m,
    AVG(e.shade_ratio) AS avg_shade_ratio
  FROM shortest s
  JOIN edges e ON s.edge = e.id
  WHERE s.edge <> -1
),
coolest_path AS (
  SELECT
    ST_LineMerge(ST_Union(e.geom)) AS geom,
    SUM(e.len_m) AS total_m,
    AVG(e.shade_ratio) AS avg_shade_ratio
  FROM coolest c
  JOIN edges e ON c.edge = e.id
  WHERE c.edge <> -1
)
SELECT
  'shortest' AS kind,
  ST_AsGeoJSON(geom) AS gj,
  total_m,
  avg_shade_ratio
FROM shortest_path
UNION ALL
SELECT
  'coolest' AS kind,
  ST_AsGeoJSON(geom) AS gj,
  total_m,
  avg_shade_ratio
FROM coolest_path;
"""

def _fetch_routes(conn_dsn, src, dst):
    with psycopg2.connect(conn_dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(SQL_ROUTE, (src[0], src[1], dst[0], dst[1]))
        rows = cur.fetchall()
        routes = {r["kind"]: r for r in rows}
        return routes

print("  • 경로 계산(pgr_dijkstra) 수행 중 …")
routes = _fetch_routes(PG_URL, SRC, DST)

# (4) Folium 레이어로 추가(지도 m는 기존에 생성된 객체를 재사용)
route_fg = folium.FeatureGroup(name="🗺️ 경로(15:00)", show=True)

# 출발/도착 마커
folium.Marker((SRC[1], SRC[0]), tooltip="출발", icon=folium.Icon(color="green")).add_to(route_fg)
folium.Marker((DST[1], DST[0]), tooltip="도착", icon=folium.Icon(color="red")).add_to(route_fg)

# 최단 경로
r_min = routes.get("shortest")
if r_min and r_min["gj"]:
    folium.GeoJson(
        r_min["gj"],
        name=f"📏 최단 • {r_min['total_m']:.0f} m • shade {(r_min['avg_shade_ratio'] or 0)*100:.1f}%",
        style_function=lambda _:{ "color":"#333333", "weight":6, "opacity":0.95 }
    ).add_to(route_fg)

# 시원길(그늘 최대 선호)
r_cool = routes.get("coolest")
if r_cool and r_cool["gj"]:
    folium.GeoJson(
        r_cool["gj"],
        name=f"🧊 시원길 • {r_cool['total_m']:.0f} m • shade {(r_cool['avg_shade_ratio'] or 0)*100:.1f}%",
        style_function=lambda _:{ "color":"#225ea8", "weight":6, "opacity":0.95 }
    ).add_to(route_fg)

m.add_child(route_fg)
folium.LayerControl(collapsed=False).add_to(m)

# (5) 추가 저장본(경로 포함)
m.save("shadow_map_pretty_15_with_routes.html")
print("shadow_map_pretty_15_with_routes.html 저장 완료")