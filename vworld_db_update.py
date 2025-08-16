# shadow_append_vworld.py
import os, math, hashlib, requests, datetime
import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon, GeometryCollection
from shapely import affinity, wkb
from sqlalchemy import create_engine, text
from geoalchemy2 import Geometry as GA_Geom
from pysolar.solar import get_altitude, get_azimuth
import pytz

# ───────── 설정 ─────────
VWORLD_KEY = os.getenv("VWORLD_KEY")
VWORLD_DOMAIN = os.getenv("VWORLD_DOMAIN", "127.0.0.1")
assert VWORLD_KEY, "VWORLD_KEY 환경변수 필요"

PG_URL = os.getenv("PG_URL", "postgresql://postgres:804009@localhost:5432/shadi")

TYPENAME = "lt_c_bldginfo"
# 유성구 궁동 인근 예시 BBOX (원하는 범위로 바꾸세요)
MINX, MINY, MAXX, MAXY =127.350154,36.360371,127.352894,36.366280
 

STAMP = "20240731_1800"
TB_BUILDING = f"shadow_building_{STAMP}"
TB_UNION    = f"shadow_union_{STAMP}"   # 라우팅에서 참조

# 높이 추정 규칙
DEFAULT_FLOOR_H = 3.0   # m
DEFAULT_HEIGHT  = 10.0  # m

# sweep 샘플링(값↑ → 경계 부드러움↑, 계산량↑)
SWEEP_STEPS = 10

# 건물 바닥(footprint) 지우기용 미세 버퍼 (단위: 미터, 5179 좌표계 기준)
BASE_ERASE_BUFFER_M = 0.05  # 5cm; 필요 시 0.1~0.2로 조정

# IoU 중복 판정 임계값 (0~1): 기존 그림자와 0.8 이상 겹치면 중복으로 간주해 skip
IOU_DUP_THRESH = 0.80

# ───────── 유틸 ─────────
def _geom_md5(g):
    if g is None or g.is_empty: return None
    return hashlib.md5(wkb.dumps(g, hex=False)).hexdigest()

def _to_multi(g):
    if g is None or g.is_empty: return None
    if isinstance(g, MultiPolygon): return g
    if isinstance(g, Polygon): return MultiPolygon([g])
    if isinstance(g, GeometryCollection):
        polys=[]
        for sub in g.geoms:
            if isinstance(sub, Polygon): polys.append(sub)
            elif isinstance(sub, MultiPolygon): polys.extend(list(sub.geoms))
        return MultiPolygon(polys) if polys else None
    return None

# ───────── VWorld 건물 수집 ─────────
def fetch_vworld_bldg(minx, miny, maxx, maxy):
    from urllib.parse import urlencode
    base = "https://api.vworld.kr/req/wfs"
    params = {
        "service": "WFS", "version": "1.1.0", "request": "GetFeature",
        "typeName": TYPENAME, "srsName": "CRS:84",
        "bbox": f"{minx},{miny},{maxx},{maxy},CRS:84",
        "outputFormat": "application/json", "count": 1000,
        "key": VWORLD_KEY, "domain": VWORLD_DOMAIN,
    }
    url = f"{base}?{urlencode(params)}"
    print("WFS URL:", url)

    r = requests.get(url, timeout=60)
    r.raise_for_status()
    js = r.json()

    feats = js.get("features") if isinstance(js, dict) else None
    if not feats:
        print("ℹ️ VWorld features=0")
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:4326")

    gdf = gpd.GeoDataFrame.from_features(feats)

    # GeoSeries → GeoDataFrame 승격
    if isinstance(gdf, gpd.GeoSeries):
        gdf = gdf.to_frame(name="__geom__")
        gdf = gpd.GeoDataFrame(gdf, geometry="__geom__", crs="EPSG:4326")

    # 활성 geometry 컬럼 확정
    if not hasattr(gdf, "geometry") or not isinstance(getattr(gdf, "geometry"), gpd.GeoSeries):
        cand = [c for c in gdf.columns if str(getattr(gdf[c], "dtype", "")) == "geometry"]
        if not cand:
            for c in gdf.columns:
                try:
                    if gdf[c].apply(lambda x: hasattr(x, "geom_type")).any():
                        cand = [c]; break
                except Exception:
                    pass
        if not cand:
            print("DEBUG columns:", list(gdf.columns))
            raise SystemExit("❌ geometry 열을 찾지 못했습니다.")
        gdf = gpd.GeoDataFrame(gdf, geometry=cand[0], crs="EPSG:4326")

    # CRS 정리
    if gdf.crs is None:
        gdf.set_crs(4326, inplace=True)
    else:
        crs_txt = str(gdf.crs).upper()
        if crs_txt in ("CRS:84", "OGC:CRS84"):
            gdf = gdf.to_crs(4326)

    # 유효성 & 멀티폴리곤화 (set_geometry 사용)
    try:
        gdf = gdf.set_geometry(gdf.make_valid().geometry)
    except Exception:
        gdf = gdf.set_geometry(gdf.buffer(0))

    gdf = gdf.set_geometry(gdf.geometry.apply(_to_multi))
    gdf = gdf[gdf.geometry.notna()]
    gdf = gdf[~gdf.geometry.is_empty]

    print(f"VWorld buildings: {len(gdf)} | geom_name: {gdf.geometry.name} | crs: {gdf.crs}")
    return gdf

# ───────── 태양 위치 ─────────
def compute_sun(dt_local, lat, lon):
    kst = pytz.timezone("Asia/Seoul")
    if dt_local.tzinfo is None:
        dt_kst = kst.localize(dt_local)
    else:
        dt_kst = dt_local.astimezone(kst)
    dt_utc = dt_kst.astimezone(pytz.utc)  # tz-aware 유지
    alt = get_altitude(lat, lon, dt_utc)
    azi = get_azimuth(lat, lon, dt_utc)
    print(f"Sun alt={alt:.2f}°, az={azi:.2f}° (KST {dt_kst}, UTC {dt_utc})")
    return alt, azi

# ───────── 그림자 생성 ─────────
def sweep_shadow(geom5179, L, dir_deg, n=SWEEP_STEPS):
    if L <= 0: return None
    rad = math.radians(dir_deg)
    dx, dy = L*math.sin(rad), L*math.cos(rad)  # x=East, y=North
    geoms = [affinity.translate(geom5179, xoff=dx*i/n, yoff=dy*i/n) for i in range(n+1)]
    out = geoms[0]
    for g in geoms[1:]:
        out = out.union(g)
    return out

def footprints_to_shadows(gdf4326, alt_deg, az_deg):
    if alt_deg <= 0:
        print("⚠️ 태양고도 ≤ 0 → 그림자 없음")
        return gpd.GeoDataFrame({"geometry":[]}, geometry="geometry", crs="EPSG:4326")

    # 그림자 진행 방향 (태양 반대)
    dir_deg = (az_deg + 180.0) % 360.0

    gdf = gdf4326.copy()

    # 높이 추정
    def _est_h(row):
        h = None
        if "height" in row and row["height"] not in (None, "", 0):
            try: h = float(row["height"])
            except: h = None
        if h is None and "grnd_flr" in row and row["grnd_flr"] not in (None, "", 0):
            try: h = float(row["grnd_flr"]) * DEFAULT_FLOOR_H
            except: h = None
        return h if (h and h > 0) else DEFAULT_HEIGHT
    gdf["est_h"] = gdf.apply(_est_h, axis=1)

    # 5179로 변환
    g5179 = gdf.to_crs(5179)

    # ⚠️ 건물 폴리곤 전체 유니온(살짝 확장) → 나중에 그림자에서 제거
    fp_union = g5179.geometry.unary_union.buffer(BASE_ERASE_BUFFER_M)

    # 그림자 길이
    alt_rad = math.radians(alt_deg)
    g5179["L"] = gdf["est_h"].apply(lambda h: h / max(math.tan(alt_rad), 1e-6))

    shadows = []
    for geom, L in zip(g5179.geometry, g5179["L"]):
        if geom is None or geom.is_empty:
            continue
        sh = sweep_shadow(geom, L, dir_deg, n=SWEEP_STEPS)
        if sh is None or sh.is_empty:
            continue

        # ✅ 건물 바닥(footprints) 제거!
        sh = sh.difference(fp_union)
        if sh.is_empty:
            continue
        shadows.append(sh)

    if not shadows:
        return gpd.GeoDataFrame({"geometry":[]}, geometry="geometry", crs="EPSG:4326")

    out5179 = gpd.GeoDataFrame(geometry=shadows, crs=5179)
    out4326 = out5179.to_crs(4326)

    # 정리
    try:
        out4326 = out4326.set_geometry(out4326.make_valid().geometry)
    except Exception:
        out4326 = out4326.set_geometry(out4326.buffer(0))
    out4326 = out4326.set_geometry(out4326.geometry.apply(_to_multi))
    out4326 = out4326[out4326.geometry.notna()]
    out4326 = out4326[~out4326.geometry.is_empty]
    if out4326.geometry.name != "geometry":
        out4326 = out4326.rename(columns={out4326.geometry.name: "geometry"}).set_geometry("geometry")

    print(f"Shadows created (footprint removed): {len(out4326)}")
    return out4326[["geometry"]]

# ───────── DB 준비/적재 ─────────
def ensure_tables(engine):
    with engine.begin() as conn:
        for tb in (TB_BUILDING, TB_UNION):
            conn.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {tb}(
                    geometry geometry(MULTIPOLYGON,4326) NOT NULL
                );
            """))
            conn.execute(text(f"ALTER TABLE {tb} ADD COLUMN IF NOT EXISTS geom_hash TEXT;"))
            conn.execute(text(f"UPDATE {tb} SET geom_hash = md5(ST_AsEWKB(geometry)) WHERE geom_hash IS NULL;"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {tb}_gix ON {tb} USING GIST(geometry);"))
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {tb}_geom_hash_idx ON {tb}(geom_hash);"))

def insert_append(engine, table, gdf, dedup_against=None, iou_thresh=IOU_DUP_THRESH):
    if gdf.empty:
        print(f"ℹ️ No geometries to insert for {table}")
        return

    STAGE = f"_stage_{table}"
    gdf2 = gdf.copy()
    gdf2["geom_hash"] = gdf2["geometry"].apply(_geom_md5)
    gdf2.to_postgis(
        STAGE, engine,
        if_exists="replace", index=False,
        dtype={"geometry": GA_Geom("MULTIPOLYGON", srid=4326)}
    )

    with engine.begin() as conn:
        conn.execute(text(f"UPDATE {STAGE} SET geom_hash = md5(ST_AsEWKB(geometry)) WHERE geom_hash IS NULL;"))

        if dedup_against:
            conn.execute(text(f"DROP TABLE IF EXISTS _keep_{table};"))
            conn.execute(text(f"""
                CREATE TEMP TABLE _keep_{table} AS
                WITH cand AS (SELECT * FROM {STAGE}),
                     ex   AS (SELECT geometry FROM {dedup_against})
                SELECT c.*
                FROM cand c
                LEFT JOIN LATERAL (
                   SELECT
                     ST_Area(ST_Intersection(c.geometry, e.geometry)) /
                     NULLIF(ST_Area(ST_Union(c.geometry, e.geometry)), 0) AS iou
                   FROM ex e
                   WHERE ST_DWithin(c.geometry, e.geometry, 0.00010) -- ~10m
                     AND ST_Intersects(c.geometry, e.geometry)
                   ORDER BY iou DESC
                   LIMIT 1
                ) m ON TRUE
                WHERE (m.iou IS NULL OR m.iou < {iou_thresh});
            """))
            conn.execute(text(f"TRUNCATE {STAGE};"))
            conn.execute(text(f"INSERT INTO {STAGE} SELECT * FROM _keep_{table};"))

        # 좌측 조인 방식 UPSERT (geom_hash 중복 방지)
        conn.execute(text(f"""
            INSERT INTO {table}(geometry, geom_hash)
            SELECT s.geometry, s.geom_hash
            FROM {STAGE} s
            LEFT JOIN {table} t ON t.geom_hash = s.geom_hash
            WHERE t.geom_hash IS NULL;
        """))

        added = conn.execute(text(f"""
            SELECT COUNT(*) FROM {STAGE} s
            LEFT JOIN {table} t ON t.geom_hash = s.geom_hash
            WHERE t.geom_hash IS NULL;
        """)).scalar()
        total = conn.execute(text(f"SELECT COUNT(*) FROM {table};")).scalar()
        print(f"✅ {table}: 새로 추가 {added}개 / 총 {total}개")

# ───────── 메인 ─────────
def main():
    gdf_b = fetch_vworld_bldg(MINX, MINY, MAXX, MAXY)

    dt_local = datetime.datetime(2024, 7, 31, 18, 0, 0)  # KST
    lat_c = (MINY + MAXY)/2.0
    lon_c = (MINX + MAXX)/2.0
    alt, az = compute_sun(dt_local, lat_c, lon_c)
    if alt <= 0:
        print("❌ 해당 시각에 태양고도 ≤ 0 → 그림자 계산 생략"); return

    gdf_shadow = footprints_to_shadows(gdf_b, alt, az)
    if gdf_shadow.empty:
        print("❌ 그림자 생성 0개"); return

    engine = create_engine(PG_URL, pool_pre_ping=True)
    ensure_tables(engine)

    insert_append(engine, TB_BUILDING, gdf_shadow, dedup_against=TB_BUILDING)
    insert_append(engine, TB_UNION,    gdf_shadow, dedup_against=TB_UNION)

if __name__ == "__main__":
    main()
