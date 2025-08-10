# load_yuseong_to_postgis.py
import osmnx as ox
import geopandas as gpd
from sqlalchemy import create_engine
from geoalchemy2 import Geometry
from shapely.geometry import LineString, MultiLineString, GeometryCollection
from shapely.ops import linemerge

# ───────── 설정 ─────────
PLACE = "Yuseong-gu, Daejeon, South Korea"
PG_URL = "postgresql://postgres:804009@localhost:5432/shadi"  # 네가 쓴 연결 문자열 그대로

# ───────── 유틸: 어떤 경우든 LINESTRING으로 맞추기 ─────────
def to_linestring(g):
    if g is None:
        return None
    if isinstance(g, LineString):
        return g
    if isinstance(g, MultiLineString):
        try:
            m = linemerge(g)
            # linemerge 결과가 MultiLineString일 수도 있어 한 번 더 처리
            if isinstance(m, MultiLineString):
                # 여러 선이 남으면 가장 긴 선만 사용 (라우팅 엣지용으로 무난)
                longest = max(list(m.geoms), key=lambda x: x.length)
                return longest
            return m
        except Exception:
            # 실패 시 가장 긴 구성요소 선택
            longest = max(list(g.geoms), key=lambda x: x.length)
            return longest
    if isinstance(g, GeometryCollection):
        # 컬렉션에서 선형 요소만 추출하여 병합
        lines = [geom for geom in g.geoms if isinstance(geom, (LineString, MultiLineString))]
        if not lines:
            return None
        merged = linemerge(lines)
        if isinstance(merged, MultiLineString):
            return max(list(merged.geoms), key=lambda x: x.length)
        return merged
    # 그 외 타입은 그대로 반환 (에지에는 거의 안 옴)
    return g

def main():
    # 1) 유성구 보행 네트워크 다운
    print("▶ Downloading walking network for:", PLACE)
    G = ox.graph_from_place(PLACE, network_type="walk", simplify=True)

    # 2) GeoDataFrame 변환
    nodes_gdf, edges_gdf = ox.graph_to_gdfs(G)

    # 3) 에지 인덱스/좌표계/길이 계산
    edges_gdf = edges_gdf.reset_index()  # u, v, key 포함
    # 좌표계 4326으로 맞춤 (OSMnx 기본도 4326이지만 안전하게 명시)
    edges_gdf = edges_gdf.to_crs(epsg=4326)
    nodes_gdf = nodes_gdf.to_crs(epsg=4326)

    # 3-1) geometry를 확실히 LINESTRING으로 보정
    print("▶ Coercing edge geometries to LINESTRING...")
    edges_gdf["geometry"] = edges_gdf["geometry"].apply(to_linestring)
    edges_gdf = edges_gdf[edges_gdf["geometry"].notnull()].copy()

    # 3-2) 길이(m) 계산 (EPSG:5179: Korea 2000 / Unified CS)
    edges_gdf["len_m"] = edges_gdf.geometry.to_crs(5179).length

    # 4) DB 연결
    print("▶ Connecting to PostGIS…")
    engine = create_engine(PG_URL)

    # 5) 컬럼명 'geom'으로 표준화 + 활성 지오메트리 설정
    edges_gdf = edges_gdf.rename(columns={"geometry": "geom"})
    edges_gdf = gpd.GeoDataFrame(edges_gdf, geometry="geom", crs="EPSG:4326")

    nodes_gdf = nodes_gdf.rename(columns={"geometry": "geom"})
    nodes_gdf = gpd.GeoDataFrame(nodes_gdf, geometry="geom", crs="EPSG:4326")

    # 6) PostGIS로 저장 (dtype 명시)
    print("▶ Writing edges (ways_raw) to PostGIS…")
    edges_gdf.to_postgis(
        "ways_raw",
        engine,
        if_exists="replace",
        index=False,
        dtype={"geom": Geometry("LINESTRING", 4326)}
    )

    print("▶ Writing nodes (nodes_raw) to PostGIS…")
    nodes_gdf.to_postgis(
        "nodes_raw",
        engine,
        if_exists="replace",
        index=False,
        dtype={"geom": Geometry("POINT", 4326)}
    )

    print("saved: ways_raw, nodes_raw")

if __name__ == "__main__":
    main()
