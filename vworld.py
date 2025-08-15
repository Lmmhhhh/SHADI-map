# vworld_getfeature.py
import os, requests, geopandas as gpd
from urllib.parse import urlencode
import folium

KEY = os.getenv("VWORLD_KEY")
DOMAIN = os.getenv("VWORLD_DOMAIN", "127.0.0.1")
assert KEY, "VWORLD_KEY 환경변수 필요"

TYPENAME = "lt_c_bldginfo"
minx, miny, maxx, maxy = 127.345869, 36.361529, 127.352926, 36.365649  # (경도min, 위도min, 경도max, 위도max)

base = "https://api.vworld.kr/req/wfs"
params = {
    "service": "WFS",
    "version": "1.1.0",
    "request": "GetFeature",
    "typeName": TYPENAME,
    "srsName": "CRS:84",
    "bbox": f"{minx},{miny},{maxx},{maxy},CRS:84",
    "outputFormat": "application/json",
    "count": 1000,
    "key": KEY,
    "domain": DOMAIN,
}
url = f"{base}?{urlencode(params)}"
print("URL:", url)

# ✅ requests로 가져와서 from_features로 변환 (GDAL 캐시 회피)
r = requests.get(url, timeout=60)
print("HTTP", r.status_code, r.headers.get("Content-Type"))
txt = r.text[:200]
if "ServiceExceptionReport" in txt or "ServiceException" in txt:
    print("서비스 에러 응답 앞부분:", txt)
    raise SystemExit("WFS 호출 실패")

js = r.json()
gdf = gpd.GeoDataFrame.from_features(js["features"], crs="EPSG:4326")

print("features:", len(gdf))
print("columns:", list(gdf.columns))
print("bbox:", gdf.total_bounds)
print(gdf.geom_type.unique())
print(gdf.crs)

# ✅ Folium 지도에 표시
center = [gdf.geometry.unary_union.centroid.y,
          gdf.geometry.unary_union.centroid.x]

m = folium.Map(location=center, zoom_start=17, tiles="CartoDB positron")

folium.GeoJson(
    gdf,
    name="Buildings",
    style_function=lambda _: {
        "fillColor": "#3182bd",
        "color": "#08519c",
        "weight": 0.5,
        "fillOpacity": 0.4
    },
    tooltip=folium.GeoJsonTooltip(fields=["bld_nm"], aliases=["건물명"])
).add_to(m)

folium.LayerControl(collapsed=False).add_to(m)

m.save("vworld_buildings.html")
print("✅ 저장 완료: vworld_buildings.html")
print("요청 BBOX:", (minx, miny, maxx, maxy))
print("응답 BBOX:", gdf.total_bounds)
