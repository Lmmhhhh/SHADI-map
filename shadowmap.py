import time, math, datetime, pytz, re, warnings
import pandas as pd, geopandas as gpd, folium, osmnx as ox
from shapely.geometry import Point, Polygon, MultiPolygon
from shapely.affinity import translate, scale, rotate
from shapely.validation import make_valid
from shapely.ops import unary_union, transform
from pysolar.solar import get_altitude, get_azimuth
from pyproj import Transformer
import numpy as np

# ────────────────────────────── 기본 설정 ──────────────────────────────
tz   = pytz.timezone("Asia/Seoul")
now  = tz.localize(datetime.datetime(2025, 7, 20, 18, 0, 0))   # 분석 시각
WIDTH_RATIO_TREE = 3.0
proj = Transformer.from_crs(4326, 5179, always_xy=True)
warnings.filterwarnings("ignore", message="I don't know about leap seconds")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="shapely")

# ────────────────────────────── 유틸 함수 ──────────────────────────────
def shadow_len(h, alt):  # 그림자 길이(m)
    return 0 if alt <= 0 else h / math.tan(math.radians(alt))

def tree_shadow_ellipse(lat, lon, r_m, alt, azi):
    if alt <= 0:
        return Polygon()
    to5179 = Transformer.from_crs(4326, 5179, always_xy=True)
    to4326 = Transformer.from_crs(5179, 4326, always_xy=True)
    x0, y0 = to5179.transform(lon, lat)
    circle = Point(x0, y0).buffer(r_m)
    stretch = 1 / math.tan(math.radians(alt))
    ellip   = scale(circle, 1, stretch, origin=(x0, y0))
    ellip   = rotate(ellip, (azi + 90) % 360, origin=(x0, y0))
    L   = shadow_len(r_m, alt)
    dx  = (L/2) * math.sin(math.radians(azi))
    dy  = (L/2) * math.cos(math.radians(azi))
    ellip = translate(ellip, xoff=dx, yoff=dy)
    shadow = transform(lambda x, y, z=None: to4326.transform(x, y), ellip)
    return make_valid(shadow)

def shelter_shadow_octagon(lat, lon, diameter_m, height_m, alt, azi):
    if alt <= 0:
        return Polygon()
    r = max(0.1, diameter_m / 2.0)  # 지붕 반지름
    angles = [math.radians(22.5 + 45*i) for i in range(8)]
    base_pts = [(r*math.cos(th), r*math.sin(th)) for th in angles]
    base = Polygon(base_pts)
    L = shadow_len(height_m, alt)
    stretch = (L / r) if r > 0 else 1.0
    shadow = scale(base, 1, stretch, origin=(0, 0))
    shadow = rotate(shadow, (azi + 90) % 360, origin=(0, 0))
    dx, dy = (L/2)*math.sin(math.radians(azi)), (L/2)*math.cos(math.radians(azi))
    shadow = translate(shadow, xoff=dx, yoff=dy)
    to5179 = Transformer.from_crs(4326, 5179, always_xy=True)
    to4326 = Transformer.from_crs(5179, 4326, always_xy=True)
    cx, cy = to5179.transform(lon, lat)
    shadow = translate(shadow, xoff=cx, yoff=cy)
    return make_valid(transform(lambda x, y, z=None: to4326.transform(x, y), shadow))

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
    if isinstance(poly, Polygon):
        shadow = _shadow(poly)
    else:
        shadow = unary_union([_shadow(g) for g in poly.geoms])
    shadow = shadow.difference(poly)
    return make_valid(shadow)

def geom_area_m2(geom):
    proj_fn = lambda x, y, z=None: proj.transform(x, y)
    return transform(proj_fn, geom).area

def to_float_or_none(val):
    num = re.sub(r"[^0-9.]", "", str(val))
    return float(num) if num else None

# ────────── 0. 충남대 3km 범위(사각) ──────────
CENTER_CNU = (36.36917, 127.34515)  # (lat, lon)
DIST_M     = 1000
deg = DIST_M / 111_320
buffer_rect = Polygon([
    (CENTER_CNU[1]-deg, CENTER_CNU[0]-deg),
    (CENTER_CNU[1]+deg, CENTER_CNU[0]-deg),
    (CENTER_CNU[1]+deg, CENTER_CNU[0]+deg),
    (CENTER_CNU[1]-deg, CENTER_CNU[0]+deg)
])

# ────────────────────────────── 시작 ──────────────────────────────
t0 = time.time()
print("▶ [START] shadowmap.py 실행")

# 0) 유성구 경계(지도의 기본 중심)
admin_gdf  = ox.geocode_to_gdf("Yuseong-gu, Daejeon, South Korea")
admin_poly = make_valid(admin_gdf.loc[0, "geometry"].buffer(0))
CENTER     = (admin_poly.centroid.y, admin_poly.centroid.x)

# 1) 가로수
print("  • 가로수 CSV 로드 중 …")
trees = pd.read_csv("data/대전광역시_가로수 현황_20221201.csv", encoding="euc-kr")
trees = trees.copy()
trees["위도"] = pd.to_numeric(trees["위도"], errors="coerce")
trees["경도"] = pd.to_numeric(trees["경도"], errors="coerce")
trees_gdf = gpd.GeoDataFrame(
    trees.dropna(subset=["경도","위도"]),
    geometry=[Point(xy) for xy in zip(trees["경도"], trees["위도"])],
    crs="EPSG:4326",
)
trees_gdf = gpd.clip(trees_gdf, buffer_rect)
print(f"    → 가로수 {len(trees_gdf):,} 개 (버퍼 범위)")

tree_layers = []
for _, r in trees_gdf.iterrows():
    lat, lon  = r["위도"], r["경도"]
    h         = float(r.get("수고", 4))
    crown_r   = h * 0.25
    alt       = get_altitude(lat, lon, now)
    azi       = (get_azimuth(lat, lon, now) + 180) % 360
    poly      = tree_shadow_ellipse(lat, lon, crown_r, alt, azi)
    if poly.is_empty: continue
    area  = geom_area_m2(poly)
    tip   = f"Tree shadow<br>H={h:.1f} m / crown≈{crown_r:.1f} m<br>{area:,.1f} ㎡"
    tree_layers.append((poly, tip))
print(f"    → 가로수 그림자 폴리곤 {len(tree_layers):,} 개 생성")

# 1-B) 쉼터 (CSV 경위도 자동 감지 + 스왑)
print("  • 그늘막 쉼터 CSV 로드 중 …")
shel = pd.read_csv("data/대전광역시 유성구_그늘막쉼터_20240920.csv", encoding="euc-kr")
shel = shel.copy()
shel["위도"] = pd.to_numeric(shel["위도"], errors="coerce")
shel["경도"] = pd.to_numeric(shel["경도"], errors="coerce")

# **자동 스왑 감지**: 위도(should ~36), 경도(should ~127)
looks_swapped = (shel["위도"].between(120, 140).mean() > 0.5) and (shel["경도"].between(30, 45).mean() > 0.5)
if looks_swapped:
    # CSV 컬럼 라벨이 뒤집혀 있음 → lon=위도, lat=경도로 사용
    shel["lon"] = shel["위도"]
    shel["lat"] = shel["경도"]
else:
    shel["lon"] = shel["경도"]
    shel["lat"] = shel["위도"]

print(f"    → 쉼터 좌표 스왑 감지: {looks_swapped}")

shel_gdf_raw = gpd.GeoDataFrame(
    shel.dropna(subset=["lat", "lon"]),
    geometry=[Point(xy) for xy in zip(shel["lon"], shel["lat"])],
    crs="EPSG:4326"
)
# 현재는 CNU 1km 사각으로 클리핑 (필요시 admin_poly로 변경 가능)
shel_gdf = gpd.clip(shel_gdf_raw, buffer_rect)
print(f"    → 쉼터 클립 전: {len(shel_gdf_raw):,} / 클립 후(버퍼 내): {len(shel_gdf):,}")

# 2-A) Shapefile 건물
print("  • Shapefile 건물 로드 중 …")
shp_gdf = (gpd.read_file("data/CH_D010_00_20250731.shp", encoding="euc-kr").to_crs(epsg=4326))
shp_gdf = shp_gdf[shp_gdf["A4"].str.contains("대전광역시", na=False)]
shp_gdf = gpd.clip(shp_gdf, buffer_rect)

shp_layers = []
for _, r in shp_gdf.iterrows():
    poly = make_valid(r.geometry)
    if poly.is_empty: continue
    floors = pd.to_numeric(r.get("A25"), errors="coerce")
    h      = floors*3 if not pd.isna(floors) else 10.0
    alt    = get_altitude(poly.centroid.y, poly.centroid.x, now)
    azi    = (get_azimuth(poly.centroid.y, poly.centroid.x, now) + 180) % 360
    s_poly = building_shadow_polygon(poly, h, alt, azi)
    if not s_poly.is_valid or s_poly.is_empty: continue
    shp_layers.append((s_poly, f"Shapefile<br>높이≈{h:.1f} m"))
print(f"    → Shapefile 그림자 {len(shp_layers):,} 개")

shp_union = unary_union([g for g, _ in shp_layers]) if len(shp_layers) > 0 else Polygon()

# 1-C) 쉼터 그림자 생성 (lon/lat 일관 사용)
shel_layers = []
for _, r in shel_gdf.iterrows():
    lat, lon = r["lat"], r["lon"]
    shelter_h = to_float_or_none(r.get("전체높이")) or 3.0
    canopy_d  = to_float_or_none(r.get("펼침지름")) or 3.0
    alt = get_altitude(lat, lon, now)
    azi = (get_azimuth(lat, lon, now) + 180) % 360
    poly = shelter_shadow_octagon(lat, lon, canopy_d, shelter_h, alt, azi)
    if poly.is_empty: continue
    area = geom_area_m2(poly)
    tip  = f"쉼터 팔각 그림자<br>지름≈{canopy_d:.1f} m<br>{area:,.1f} ㎡"
    shel_layers.append((poly, tip))
print(f"    → 쉼터 그림자 폴리곤 {len(shel_layers):,} 개 생성 (DB 저장 대상)")

# 2-B) OSM 건물
print("  • OSM 건물 로드 중 …")
try:
    osm = ox.features_from_polygon(buffer_rect, tags={"building": True})
except ox._errors.InsufficientResponseError:
    osm = ox.features_from_point(CENTER_CNU, dist=DIST_M, tags={"building": True})
osm = osm.to_crs(epsg=4326)
print(f"    → OSM 건물 {len(osm):,} 개")

osm_layers = []
for _, row in osm.iterrows():
    poly = make_valid(row.geometry)
    if poly.is_empty or (len(shp_layers) > 0 and poly.intersects(shp_union)):
        continue
    h_height = to_float_or_none(row.get("height"))
    raw_lv   = row.get("building:levels")
    lv_list  = [int(x) for x in re.findall(r'\d+', str(raw_lv) if raw_lv else "")]
    h_levels = (max(lv_list) * 3) if lv_list else None
    candidates = [h for h in (h_height, h_levels) if h is not None]
    h = max(candidates) if candidates else 10.0
    alt = get_altitude(poly.centroid.y, poly.centroid.x, now)
    azi = (get_azimuth(poly.centroid.y, poly.centroid.x, now) + 180) % 360
    if not isinstance(poly, (Polygon, MultiPolygon)): continue
    s_poly = building_shadow_polygon(poly, h, alt, azi)
    if s_poly.is_empty or not s_poly.is_valid: continue
    tooltip = f"OSM<br>높이≈{h:.1f} m"
    osm_layers.append((s_poly, tooltip))
print(f"    → OSM 그림자 {len(osm_layers):,} 개")

# 3) Folium 시각화
print("  • Folium 지도 생성 중 …")
m = folium.Map(location=CENTER, zoom_start=15, tiles=None)
folium.TileLayer("OpenStreetMap", name="Default").add_to(m)
folium.TileLayer("CartoDB positron", name="Light").add_to(m)

# 건물
bld_fg = folium.FeatureGroup(name="🏢 건물 그림자", show=False)
for poly, tip in shp_layers + osm_layers:
    folium.GeoJson(
        poly.__geo_interface__,
        style_function=lambda x: {"fillColor": "#28252c","color": "#463f4f","weight": 0.5,"fillOpacity": 0.5},
        tooltip=tip
    ).add_to(bld_fg)
bld_fg.add_to(m)

# 나무
tree_fg = folium.FeatureGroup(name="🌳 나무 그림자", show=True)
for _, r in trees_gdf.iterrows():
    lat, lon = r["위도"], r["경도"]
    alt = get_altitude(lat, lon, now)
    azi = (get_azimuth(lat, lon, now) + 180) % 360
    poly = tree_shadow_ellipse(lat, lon, 6/2, alt, azi)
    if poly.is_empty: continue
    folium.GeoJson(
        poly.__geo_interface__,
        style_function=lambda _: {"fillColor": "#7fc97f","color": "#4daf4a","weight": 0.3,"fillOpacity": 0.6}
    ).add_to(tree_fg)
tree_fg.add_to(m)

# 쉼터
shelter_fg = folium.FeatureGroup(name="⛱️ 쉼터 그림자", show=True)
for _, r in shel_gdf.iterrows():
    lon = r["lon"]; lat = r["lat"]
    shelter_h = to_float_or_none(r.get("전체높이")) or 2.5
    canopy_d  = to_float_or_none(r.get("펼침지름")) or 2.0
    alt = get_altitude(lat, lon, now)
    azi = (get_azimuth(lat, lon, now) + 180) % 360
    poly = shelter_shadow_octagon(lat, lon, canopy_d, shelter_h, alt, azi)
    if poly.is_empty: continue
    folium.GeoJson(
        poly.__geo_interface__,
        style_function=lambda _: {"fillColor": "#fdae61","color": "#e66101","weight": 0.3,"fillOpacity": 0.6},
        tooltip=f"쉼터 그림자<br>높이≈{shelter_h} m / 지름≈{canopy_d} m"
    ).add_to(shelter_fg)
shelter_fg.add_to(m)

# 4) PostGIS 저장
from sqlalchemy import create_engine, text
PG_URL = "postgresql://postgres:804009@localhost:5432/shadi"
engine = create_engine(PG_URL)

gdf_building = gpd.GeoDataFrame(geometry=[g for g, _ in (shp_layers + osm_layers)], crs="EPSG:4326")
gdf_tree     = gpd.GeoDataFrame(geometry=[g for g, _ in tree_layers], crs="EPSG:4326")
gdf_shelter  = gpd.GeoDataFrame(geometry=[g for g, _ in shel_layers], crs="EPSG:4326")

print("  • DB 저장 예정(건물/나무/쉼터):", len(gdf_building), len(gdf_tree), len(gdf_shelter))
gdf_building.to_postgis("shadow_building_20250720_1800", engine, if_exists="replace", index=False)
gdf_tree.to_postgis("shadow_tree_20250720_1800", engine, if_exists="replace", index=False)
gdf_shelter.to_postgis("shadow_shelter_20250720_1800", engine, if_exists="replace", index=False)

with engine.begin() as conn:
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS shadow_building_20250720_1800_gix
          ON shadow_building_20250720_1800 USING GIST (geometry);
        CREATE INDEX IF NOT EXISTS shadow_tree_20250720_1800_gix
          ON shadow_tree_20250720_1800 USING GIST (geometry);
        CREATE INDEX IF NOT EXISTS shadow_shelter_20250720_1800_gix
          ON shadow_shelter_20250720_1800 USING GIST (geometry);
    """))
    conn.execute(text("DROP TABLE IF EXISTS shadow_union_20250720_1800;"))
    conn.execute(text("""
        CREATE TABLE shadow_union_20250720_1800 AS
        SELECT ST_UnaryUnion(geometry) AS geometry
        FROM (
          SELECT geometry FROM shadow_building_20250720_1800
          UNION ALL
          SELECT geometry FROM shadow_tree_20250720_1800
          UNION ALL
          SELECT geometry FROM shadow_shelter_20250720_1800
        ) s;
    """))
    conn.execute(text("""
        CREATE INDEX IF NOT EXISTS shadow_union_20250720_1800_gix
          ON shadow_union_20250720_1800 USING GIST (geometry);
    """))

print("PostGIS 저장 및 UNION 테이블 생성 완료")

# 5) 경로 산출(ways_walk 사용)
import psycopg2
from psycopg2.extras import RealDictCursor
SRC = (127.345658,36.364793)  # (lon, lat)
DST = (127.341099, 36.367891) # (lon, lat)
COOL_WEIGHT = 0.8

SQL_ROUTE = f"""
WITH
u AS (
  SELECT geometry FROM shadow_union_20250720_1800 LIMIT 1
),
edges AS (
  SELECT
    w.id, w.source, w.target, w.geom, w.len_m,
    COALESCE(
      ST_Length(ST_Intersection(w.geom::geography, u.geometry::geography))
      / NULLIF(ST_Length(w.geom::geography), 0), 0
    ) AS shade_ratio
  FROM ways_walk w
  LEFT JOIN u ON ST_Intersects(w.geom, u.geometry)
),
src AS (
  SELECT v.id
  FROM ways_raw_vertices_pgr v
  JOIN (
    SELECT source AS vid FROM ways_walk
    UNION
    SELECT target AS vid FROM ways_walk
  ) ok ON ok.vid = v.id
  ORDER BY v.the_geom <-> ST_SetSRID(ST_Point(%s,%s), 4326)
  LIMIT 1
),
dst AS (
  SELECT v.id
  FROM ways_raw_vertices_pgr v
  JOIN (
    SELECT source AS vid FROM ways_walk
    UNION
    SELECT target AS vid FROM ways_walk
  ) ok ON ok.vid = v.id
  ORDER BY v.the_geom <-> ST_SetSRID(ST_Point(%s,%s), 4326)
  LIMIT 1
),
shortest AS (
  SELECT * FROM pgr_dijkstra(
    $q$
    SELECT id, source, target, len_m AS cost, len_m AS reverse_cost
    FROM ways_walk
    $q$,
    (SELECT id FROM src), (SELECT id FROM dst), false
  )
),
coolest AS (
  SELECT * FROM pgr_dijkstra(
    $q$
    SELECT
      id, source, target,
      GREATEST(len_m * (1 - {COOL_WEIGHT} * shade_ratio), 0.1) AS cost,
      GREATEST(len_m * (1 - {COOL_WEIGHT} * shade_ratio), 0.1) AS reverse_cost
    FROM (
      SELECT
        w.id, w.source, w.target, w.geom, w.len_m,
        COALESCE(
          ST_Length(ST_Intersection(w.geom::geography, su.geometry::geography))
          / NULLIF(ST_Length(w.geom::geography), 0), 0
        ) AS shade_ratio
      FROM ways_walk w
      LEFT JOIN shadow_union_20250720_1800 su
        ON ST_Intersects(w.geom, su.geometry)
    ) in_edges
    $q$,
    (SELECT id FROM src), (SELECT id FROM dst), false
  )
),
shortest_path AS (
  SELECT ST_LineMerge(ST_Union(e.geom)) AS geom, SUM(e.len_m) AS total_m, AVG(e.shade_ratio) AS avg_shade_ratio
  FROM shortest s JOIN edges e ON s.edge = e.id WHERE s.edge <> -1
),
coolest_path AS (
  SELECT ST_LineMerge(ST_Union(e.geom)) AS geom, SUM(e.len_m) AS total_m, AVG(e.shade_ratio) AS avg_shade_ratio
  FROM coolest c JOIN edges e ON c.edge = e.id WHERE c.edge <> -1
)
SELECT 'shortest' AS kind, ST_AsGeoJSON(geom) AS gj, total_m, avg_shade_ratio FROM shortest_path
UNION ALL
SELECT 'coolest'  AS kind, ST_AsGeoJSON(geom) AS gj, total_m, avg_shade_ratio FROM coolest_path;
"""

def _fetch_routes(conn_dsn, src, dst):
    with psycopg2.connect(conn_dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(SQL_ROUTE, (src[0], src[1], dst[0], dst[1]))
        rows = cur.fetchall()
        return {r["kind"]: r for r in rows}

PG_URL = "postgresql://postgres:804009@localhost:5432/shadi"
print("  • 경로 계산(pgr_dijkstra) 수행 중 …")
routes = _fetch_routes(PG_URL, SRC, DST)
r_min  = routes.get("shortest")
r_cool = routes.get("coolest")

# ✅ 경로 생성 여부 로그 추가
print(f"    → 최단 경로 생성됨: {bool(r_min and r_min.get('gj'))}")
print(f"    → 시원길 경로 생성됨: {bool(r_cool and r_cool.get('gj'))}")

# 6) 경로 레이어
poi_fg = folium.FeatureGroup(name="📍 출발/도착", show=True)
folium.Marker((SRC[1], SRC[0]), tooltip="출발", icon=folium.Icon(color="green")).add_to(poi_fg)
folium.Marker((DST[1], DST[0]), tooltip="도착", icon=folium.Icon(color="red")).add_to(poi_fg)
poi_fg.add_to(m)

if r_min and r_min["gj"]:
    fg_short = folium.FeatureGroup(
        name=f"📏 최단경로 • {r_min['total_m']:.0f} m • shade {(r_min['avg_shade_ratio'] or 0)*100:.1f}%", show=True
    )
    folium.GeoJson(r_min["gj"], style_function=lambda _:{ "color":"#333333", "weight":6, "opacity":0.95 }).add_to(fg_short)
    fg_short.add_to(m)

if r_cool and r_cool["gj"]:
    fg_cool = folium.FeatureGroup(
        name=f"🧊 그늘길경로 • {r_cool['total_m']:.0f} m • shade {(r_cool['avg_shade_ratio'] or 0)*100:.1f}%", show=True
    )
    folium.GeoJson(r_cool["gj"], style_function=lambda _:{ "color":"#225ea8", "weight":6, "opacity":0.95 }).add_to(fg_cool)
    fg_cool.add_to(m)

folium.LayerControl(collapsed=False).add_to(m)

# 7) 저장
out = "shadow_map_pretty_18_with_routes.html"
m.save(out)
print(out, "저장 완료")
print(f"총 소요: {time.time()-t0:.1f}s")
