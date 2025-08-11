# save_shadows_to_postgis.py  
import geopandas as gpd 
from shapely.ops import unary_union
from sqlalchemy import create_engine
# 9시로 실행된 shadowmap에서 리스트를 import
from shadowmap import shp_layers, osm_layers, tree_layers, shel_layers

def layers_to_gdf(layers):
    geoms = [g for g, _ in layers if g is not None and not g.is_empty]
    return gpd.GeoDataFrame(geometry=geoms, crs="EPSG:4326")

gdf_bld  = layers_to_gdf(shp_layers + osm_layers)
gdf_tree = layers_to_gdf(tree_layers)
gdf_shel = layers_to_gdf(shel_layers)

# (중요) unary_union → buffer(0)로 유효성 보정 후 GeoDataFrame로 감싸기
union_geom = unary_union(
    list(gdf_bld.geometry) + list(gdf_tree.geometry) + list(gdf_shel.geometry)
).buffer(0)

gdf_all = gpd.GeoDataFrame(geometry=[union_geom], crs="EPSG:4326")

engine = create_engine("postgresql://postgres:804009@localhost:5432/shadi")
gdf_all.to_postgis("shadow_union_20240731_0900", engine, if_exists="replace", index=False)
print("wrote: shadow_union_20240731_0900")