# server.py
# Flask backend with Kakao Map UI
# Adds: /shadow endpoint with kind={union|building|tree|shelter} to stream polygons by layer, clipped to bbox.
# Front-end shows color-coded overlays:
#   - Building: dark gray  (#28252c fill, #463f4f stroke)
#   - Tree:     green      (#7fc97f fill, #4daf4a stroke)
#   - Shelter:  orange     (#fdae61 fill, #e66101 stroke)
#
# Prereqs:
#   pip install flask flask-cors psycopg2-binary
#
# Run:  python server.py  → http://127.0.0.1:8000

import os, re, json, datetime
from urllib.parse import unquote
from flask import Flask, request, jsonify, Response, render_template_string
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor

APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = int(os.getenv("APP_PORT", "8000"))
PG_URL   = os.getenv("PG_URL",  "postgresql://postgres:804009@localhost:5432/shadi")

DEFAULT_STAMP = "20240731_1800"  # → shadow_*_20240731_1800
DEFAULT_COOL_WEIGHT = float(os.getenv("COOL_WEIGHT", "0.8"))
KAKAO_JS_KEY = os.getenv("KAKAO_JS_KEY", "")

# ---------- Helpers ----------
def _parse_coord_pair(s: str):
    """Accept 'lat,lon' or 'lon,lat' and normalize to (lon, lat). Return None if invalid."""
    if not s:
        return None
    s = s.strip()
    m = re.match(r'^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$', s)
    if not m:
        return None
    a = float(m.group(1))
    b = float(m.group(2))
    if abs(a) <= 90 and abs(b) <= 180 and abs(a) < abs(b):
        lat, lon = a, b  # "lat,lon"
    else:
        lon, lat = a, b  # "lon,lat"
    return (lon, lat)

def _stamp_from_time(t: str|None) -> str:
    """'YYYY-MM-DDTHH:MM' → 'YYYYMMDD_HHMM'"""
    if not t:
        return DEFAULT_STAMP
    try:
        t = t.replace("T", " ").strip()
        dt = datetime.datetime.strptime(t, "%Y-%m-%d %H:%M")
        return dt.strftime("%Y%m%d_%H%M")
    except Exception:
        return DEFAULT_STAMP

def _validate_table(prefix: str, stamp: str) -> str:
    """prefix in {'shadow_union','shadow_building','shadow_tree','shadow_shelter'}"""
    if prefix not in {"shadow_union","shadow_building","shadow_tree","shadow_shelter"}:
        prefix = "shadow_union"
    if not re.fullmatch(r"\d{8}_\d{4}", stamp):
        stamp = DEFAULT_STAMP
    return f"{prefix}_{stamp}"

# ---------- DB funcs ----------
def _fetch_routes(conn_dsn: str, src: tuple, dst: tuple, union_table: str, cool_weight: float):
    union_table = _validate_table("shadow_union", union_table.split("_", 2)[-1])

    conninfo = conn_dsn + "?application_name=shadi_route_srv"
    with psycopg2.connect(conninfo) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SET LOCAL statement_timeout = '60s';
            SET LOCAL idle_in_transaction_session_timeout = '30s';
            SET LOCAL jit = OFF;
            SET LOCAL work_mem = '256MB';
        """)

        q_edges = f"""
    DROP TABLE IF EXISTS edges_tmp;
    CREATE TEMP TABLE edges_tmp AS
    WITH
    params AS (
      SELECT 4.5::float8 AS shade_tol_m,
             150::float8 AS near_m,
             600::float8 AS corridor_m
    ),
    u5179 AS (
      SELECT ST_Subdivide(
               ST_MakeValid(ST_Transform(geometry, 5179)), 256
             ) AS g5179
      FROM {union_table}
    ),
    srcpt AS (SELECT ST_SetSRID(ST_Point(%s,%s), 4326) AS g4326),
    dstpt AS (SELECT ST_SetSRID(ST_Point(%s,%s), 4326) AS g4326),
    corridor AS (
      SELECT ST_Buffer(
               ST_Transform(
                 ST_MakeLine((SELECT g4326 FROM srcpt),(SELECT g4326 FROM dstpt)),
                 5179
               ),
               (SELECT corridor_m FROM params)
             ) AS g5179
    ),
    u_clip AS (
      SELECT u.g5179
      FROM u5179 u
      JOIN corridor c ON ST_Intersects(u.g5179, c.g5179)
    ),
    edges AS (
      SELECT
        w.id, w.source, w.target, w.geom, w.len_m,
        LEAST(
          COALESCE(
            SUM(
              ST_Length(
                ST_Intersection(
                  ST_SnapToGrid(ST_Transform(w.geom, 5179), 0.05),
                  ST_Buffer(u.g5179, (SELECT shade_tol_m FROM params))
                )
              )
            ) / NULLIF(
              ST_Length(ST_SnapToGrid(ST_Transform(w.geom, 5179), 0.05)), 0
            ),
            0
          ),
          1.0
        ) AS shade_ratio
      FROM ways_raw w
      JOIN corridor c
        ON ST_Intersects(ST_Transform(w.geom, 5179), c.g5179)
      LEFT JOIN u_clip u
        ON ST_DWithin(ST_Transform(w.geom, 5179), u.g5179, (SELECT near_m FROM params))
      GROUP BY w.id, w.source, w.target, w.geom, w.len_m
    )
    SELECT id, source, target, geom, len_m, shade_ratio
    FROM edges;
"""
        cur.execute(q_edges, (src[0], src[1], dst[0], dst[1]))
        cur.execute("CREATE INDEX IF NOT EXISTS edges_tmp_id_idx ON edges_tmp(id);")
        cur.execute("CREATE INDEX IF NOT EXISTS edges_tmp_st_idx ON edges_tmp(source, target);")

        sql_route = f"""
        WITH
        ok_v AS (
          SELECT v.id, v.the_geom
          FROM ways_raw_vertices_pgr v
          JOIN (
            SELECT source AS vid FROM ways_raw
            UNION
            SELECT target AS vid FROM ways_raw
          ) ok ON ok.vid = v.id
        ),
        src AS (
          SELECT id FROM ok_v
          ORDER BY the_geom <-> ST_SetSRID(ST_Point(%s,%s), 4326)
          LIMIT 1
        ),
        dst AS (
          SELECT id FROM ok_v
          ORDER BY the_geom <-> ST_SetSRID(ST_Point(%s,%s), 4326)
          LIMIT 1
        ),
        shortest AS (
          SELECT * FROM pgr_dijkstra(
            $$SELECT id, source, target, len_m AS cost, len_m AS reverse_cost FROM edges_tmp$$,
            (SELECT id FROM src), (SELECT id FROM dst), false
          )
        ),
        coolest AS (
          SELECT * FROM pgr_dijkstra(
            $$SELECT
                 id, source, target,
                 GREATEST(len_m * (1 - {cool_weight} * shade_ratio), 0.1) AS cost,
                 GREATEST(len_m * (1 - {cool_weight} * shade_ratio), 0.1) AS reverse_cost
              FROM edges_tmp$$,
            (SELECT id FROM src), (SELECT id FROM dst), false
          )
        ),
        shortest_path AS (
          SELECT ST_LineMerge(ST_Union(e.geom)) AS geom,
                 SUM(e.len_m) AS total_m,
                 AVG(e.shade_ratio) AS avg_shade_ratio
          FROM shortest s
          JOIN edges_tmp e ON s.edge = e.id
          WHERE s.edge <> -1
        ),
        coolest_path AS (
          SELECT ST_LineMerge(ST_Union(e.geom)) AS geom,
                 SUM(e.len_m) AS total_m,
                 AVG(e.shade_ratio) AS avg_shade_ratio
          FROM coolest c
          JOIN edges_tmp e ON c.edge = e.id
          WHERE c.edge <> -1
        )
        SELECT 'shortest' AS kind, ST_AsGeoJSON(geom) AS gj, total_m, avg_shade_ratio FROM shortest_path
        UNION ALL
        SELECT 'coolest'  AS kind, ST_AsGeoJSON(geom) AS gj, total_m, avg_shade_ratio FROM coolest_path;
        """
        cur.execute(sql_route, (src[0], src[1], dst[0], dst[1]))
        rows = cur.fetchall()

    out = {}
    for r in rows:
        gj = json.loads(r["gj"]) if r["gj"] else None
        out[r["kind"]] = {
            "gj": gj,
            "total_m": float(r["total_m"]) if r["total_m"] is not None else None,
            "avg_shade_ratio": float(r["avg_shade_ratio"]) if r["avg_shade_ratio"] is not None else None
        }
    return out

def _fetch_shadow_any(conn_dsn: str, table_prefix: str, stamp: str,
                      bbox: tuple|None, simplify_tol_m: float = 0.7):
    table = _validate_table(table_prefix, stamp)
    conninfo = conn_dsn + "?application_name=shadi_shadow_srv"
    with psycopg2.connect(conninfo) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SET LOCAL statement_timeout = '60s';
            SET LOCAL jit = OFF;
            SET LOCAL work_mem = '256MB';
        """)

        if bbox:
            minx, miny, maxx, maxy = bbox
            q = f"""
            WITH bb AS (SELECT ST_MakeEnvelope(%s,%s,%s,%s,4326) AS env4326),
                 src AS (SELECT ST_MakeValid(geometry) AS g4326 FROM {table}),
                 clip AS (
                   SELECT ST_Intersection(s.g4326, bb.env4326) AS g4326
                   FROM src s, bb WHERE ST_Intersects(s.g4326, bb.env4326)
                 ),
                 fix AS (
                   SELECT ST_CollectionExtract(ST_Buffer(g4326,0),3) AS g4326
                   FROM clip WHERE g4326 IS NOT NULL AND NOT ST_IsEmpty(g4326)
                 ),
                 simp AS (
                   SELECT ST_Buffer(
                            ST_SimplifyPreserveTopology(
                              ST_SnapToGrid(ST_Transform(g4326,5179),0.05),
                              %s
                            ), 0
                          ) AS g5179
                   FROM fix
                 ),
                 u AS (
                   SELECT ST_Buffer(ST_UnaryUnion(ST_Collect(g5179)),0) AS g5179
                   FROM simp
                 )
            SELECT ST_AsGeoJSON(ST_Transform(u.g5179,4326)) AS gj,
                   (SELECT COUNT(*) FROM simp) AS cnt
            FROM u;
            """
            try:
                cur.execute(q, (minx, miny, maxx, maxy, simplify_tol_m))
            except Exception:
                conn.rollback()
                cur.execute("""
                    SET LOCAL statement_timeout = '60s';
                    SET LOCAL jit = OFF;
                    SET LOCAL work_mem = '256MB';
                """)
                q_fb = f"""
                WITH bb AS (SELECT ST_MakeEnvelope(%s,%s,%s,%s,4326) AS env4326),
                     src AS (SELECT ST_MakeValid(geometry) AS g4326 FROM {table}),
                     clip AS (
                       SELECT ST_Intersection(s.g4326, bb.env4326) AS g4326
                       FROM src s, bb WHERE ST_Intersects(s.g4326, bb.env4326)
                     ),
                     fix AS (
                       SELECT ST_CollectionExtract(ST_Buffer(g4326,0),3) AS g4326
                       FROM clip WHERE g4326 IS NOT NULL AND NOT ST_IsEmpty(g4326)
                     ),
                     simp AS (
                       SELECT ST_Buffer(
                                ST_SimplifyPreserveTopology(
                                  ST_SnapToGrid(ST_Transform(g4326,5179),0.05),
                                  %s
                                ), 0
                              ) AS g5179
                       FROM fix
                     )
                SELECT ST_AsGeoJSON(ST_Transform(ST_Collect(g5179),4326)) AS gj,
                       COUNT(*) AS cnt
                FROM simp;
                """
                cur.execute(q_fb, (minx, miny, maxx, maxy, simplify_tol_m))
        else:
            q = f"""
            WITH src AS (SELECT ST_MakeValid(geometry) AS g4326 FROM {table}),
                 fix AS (
                   SELECT ST_CollectionExtract(ST_Buffer(g4326,0),3) AS g4326
                   FROM src WHERE g4326 IS NOT NULL AND NOT ST_IsEmpty(g4326)
                 ),
                 simp AS (
                   SELECT ST_Buffer(
                            ST_SimplifyPreserveTopology(
                              ST_SnapToGrid(ST_Transform(g4326,5179),0.05),
                              %s
                            ), 0
                          ) AS g5179
                   FROM fix
                 ),
                 u AS (
                   SELECT ST_Buffer(ST_UnaryUnion(ST_Collect(g5179)),0) AS g5179
                   FROM simp
                 )
            SELECT ST_AsGeoJSON(ST_Transform(u.g5179,4326)) AS gj,
                   (SELECT COUNT(*) FROM simp) AS cnt;
            """
            try:
                cur.execute(q, (simplify_tol_m,))
            except Exception:
                conn.rollback()
                cur.execute("""
                    SET LOCAL statement_timeout = '60s';
                    SET LOCAL jit = OFF;
                    SET LOCAL work_mem = '256MB';
                """)
                q_fb = f"""
                WITH src AS (SELECT ST_MakeValid(geometry) AS g4326 FROM {table}),
                     fix AS (
                       SELECT ST_CollectionExtract(ST_Buffer(g4326,0),3) AS g4326
                       FROM src WHERE g4326 IS NOT NULL AND NOT ST_IsEmpty(g4326)
                     ),
                     simp AS (
                       SELECT ST_Buffer(
                                ST_SimplifyPreserveTopology(
                                  ST_SnapToGrid(ST_Transform(g4326,5179),0.05),
                                  %s
                                ), 0
                              ) AS g5179
                       FROM fix
                     )
                SELECT ST_AsGeoJSON(ST_Transform(ST_Collect(g5179),4326)) AS gj,
                       COUNT(*) AS cnt
                FROM simp;
                """
                cur.execute(q_fb, (simplify_tol_m,))

        row = cur.fetchone()
        gj = json.loads(row["gj"]) if row and row["gj"] else None
        cnt = int(row["cnt"]) if row and row["cnt"] is not None else 0
        return {"gj": gj, "count": cnt, "table": table}


# ---------- Flask App ----------
app = Flask(__name__)
CORS(app)

MAP_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>그늘길 라우팅 데모</title>
  <!-- autoload=false + defer -->
  <script defer src="https://dapi.kakao.com/v2/maps/sdk.js?appkey={{ kakao_js_key }}&autoload=false"></script>
  <style>
    html, body, #map { height: 100%; margin: 0; }
    #panel {
      position:absolute; top:10px; right:10px; z-index:1000;
      background:rgba(255,255,255,.96); padding:12px; border-radius:12px;
      box-shadow:0 2px 12px rgba(0,0,0,.15); width:360px;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, 'Noto Sans KR', Arial, sans-serif;
    }
    #panel h3 { margin:0 0 8px 0; font-size:16px }
    #panel label { font-size:12px; color:#333 }
    #panel input[type=text], #panel input[type=datetime-local] {
      width:100%; padding:6px 8px; margin:4px 0 8px;
      border:1px solid #ccc; border-radius:8px;
    }
    #panel .row{display:flex; gap:6px; margin-top:4px; align-items:center}
    #panel button{flex:1; padding:8px; border:0; border-radius:8px; cursor:pointer}
    #panel .btn{background:#225ea8; color:#fff}
    #panel .ghost{background:#f1f3f5; color:#222}
    #panel small{color:#666}
    #stats, #shadow-stats {font-size:12px; color:#333; margin-top:6px; line-height:1.4}
    .legend {position:absolute; bottom:14px; right:14px; z-index:1000; background:rgba(255,255,255,.9); padding:6px 10px; border-radius:8px; box-shadow:0 2px 12px rgba(0,0,0,.15); font-size:12px}
    .chip{display:inline-block; width:12px; height:12px; border-radius:3px; margin-right:6px; vertical-align:middle}
  </style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <label>출발 (lat, lon)</label>
  <input id="src" type="text" placeholder="예: 36.361738, 127.344776">
  <label>도착 (lat, lon)</label>
  <input id="dst" type="text" placeholder="예: 36.372113, 127.345180">
  <label>분석시각</label>
  <input id="time" type="datetime-local" value="2024-07-31T18:00">
  <div class="row">
    <button class="ghost" id="pick-src">지도에서 출발 찍기</button>
    <button class="ghost" id="pick-dst">지도에서 도착 찍기</button>
  </div>
  <div class="row">
    <button class="btn" id="run">실행</button>
    <button class="ghost" id="clear">초기화</button>
  </div>
  <div id="stats"></div>
  <hr style="margin:8px 0;border:0;border-top:1px solid #e5e7eb">
  <div style="font-size:13px; font-weight:600; margin-bottom:4px">그림자 레이어</div>
  <div class="row" style="gap:10px; justify-content:space-between">
    <label style="display:flex; align-items:center; gap:6px; flex:1">
      <input type="checkbox" id="toggle-building" checked>
      <span><span class="chip" style="background:#28252c; border:1px solid #463f4f"></span>건물</span>
    </label>
    <label style="display:flex; align-items:center; gap:6px; flex:1">
      <input type="checkbox" id="toggle-tree" checked>
      <span><span class="chip" style="background:#7fc97f; border:1px solid #4daf4a"></span>가로수</span>
    </label>
    <label style="display:flex; align-items:center; gap:6px; flex:1">
      <input type="checkbox" id="toggle-shelter" checked>
      <span><span class="chip" style="background:#fdae61; border:1px solid #e66101"></span>쉼터</span>
    </label>
    <button class="ghost" id="refresh-shadow" style="flex:0 0 auto">갱신</button>
  </div>
  <div id="shadow-stats"></div>
  <small>Tip: 지도 클릭으로 출발/도착을 쉽게 찍을 수 있어요.</small>
</div>
<div class="legend">
  <div><span class="chip" style="background:#333"></span> 최단 경로</div>
  <div><span class="chip" style="background:#225ea8"></span> 시원한 경로</div>
</div>

<script>
  // Kakao SDK가 로드된 뒤에만 실행되도록 보장
  window.addEventListener('DOMContentLoaded', () => {
    kakao.maps.load(() => {
      // --- Kakao Map init (center: 충남대 정문) ---
      const mapContainer = document.getElementById('map');
      const mapOption = {
        center: new kakao.maps.LatLng(36.36917, 127.34515),
        level: 4
      };
      const map = new kakao.maps.Map(mapContainer, mapOption);

      // markers & layers
      let srcMarker=null, dstMarker=null, pickMode=null;
      let shortestPolyline=null, coolestPolyline=null;
      let shadowBuilding=[], shadowTree=[], shadowShelter=[];

      function llstrLL(lat, lng){ return lat.toFixed(6)+", "+lng.toFixed(6); }
      function toLonLat(str){
        const m = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/.exec(str||"");
        if(!m) return null;
        const a=parseFloat(m[1]), b=parseFloat(m[2]);
        let lat,lon;
        if(Math.abs(a)<=90 && Math.abs(b)<=180 && Math.abs(a)<Math.abs(b)){ lat=a; lon=b; } else { lon=a; lat=b; }
        return [lon,lat];
      }

      function setSrcByLatLng(lat, lng){
        if(srcMarker) srcMarker.setMap(null);
        srcMarker = new kakao.maps.Marker({ position: new kakao.maps.LatLng(lat, lng) });
        srcMarker.setMap(map);
        document.getElementById('src').value = llstrLL(lat, lng);
      }
      function setDstByLatLng(lat, lng){
        if(dstMarker) dstMarker.setMap(null);
        dstMarker = new kakao.maps.Marker({ position: new kakao.maps.LatLng(lat, lng) });
        dstMarker.setMap(map);
        document.getElementById('dst').value = llstrLL(lat, lng);
      }

      document.getElementById('pick-src').onclick = function(){ pickMode='src'; this.style.opacity=1; document.getElementById('pick-dst').style.opacity=.8; };
      document.getElementById('pick-dst').onclick = function(){ pickMode='dst'; this.style.opacity=1; document.getElementById('pick-src').style.opacity=.8; };
      document.getElementById('clear').onclick    = function(){
        if(srcMarker) srcMarker.setMap(null); if(dstMarker) dstMarker.setMap(null);
        srcMarker=dstMarker=null;
        if(shortestPolyline) shortestPolyline.setMap(null);
        if(coolestPolyline) coolestPolyline.setMap(null);
        shortestPolyline=coolestPolyline=null;
        clearPolygons(shadowBuilding); clearPolygons(shadowTree); clearPolygons(shadowShelter);
        document.getElementById('src').value=''; document.getElementById('dst').value='';
        document.getElementById('pick-src').style.opacity=1; document.getElementById('pick-dst').style.opacity=1; pickMode=null;
        document.getElementById('stats').innerHTML = "";
      };

      kakao.maps.event.addListener(map, 'click', function(mouseEvent){
        if(!pickMode) return;
        const latlng = mouseEvent.latLng;
        if(pickMode==='src') setSrcByLatLng(latlng.getLat(), latlng.getLng());
        else setDstByLatLng(latlng.getLat(), latlng.getLng());
      });

      document.getElementById('run').onclick = async function(){
        const s = toLonLat(document.getElementById('src').value);
        const d = toLonLat(document.getElementById('dst').value);
        const t = document.getElementById('time').value;
        if(!s || !d){ alert("좌표 형식이 올바르지 않습니다. 예: 36.361738, 127.344776"); return; }
        const qs = new URLSearchParams({
          src: s[1].toFixed(6)+","+s[0].toFixed(6),
          dst: d[1].toFixed(6)+","+d[0].toFixed(6),
          time: t || ""
        }).toString();

        document.getElementById('run').disabled = true;
        document.getElementById('run').innerText = "계산중...";
        try{
          const res = await fetch('/route?'+qs, {method:'GET'});
          const js  = await res.json();
          if(!res.ok){ throw new Error(js.error || "route API 실패"); }

          if(shortestPolyline) shortestPolyline.setMap(null);
          if(coolestPolyline) coolestPolyline.setMap(null);

          if(js.shortest && js.shortest.gj){
            shortestPolyline = polylineFromGeoJSON(js.shortest.gj, {strokeWeight:6, strokeColor:'#333333', strokeOpacity:0.95});
            shortestPolyline.setMap(map);
          }
          if(js.coolest && js.coolest.gj){
            coolestPolyline  = polylineFromGeoJSON(js.coolest.gj, {strokeWeight:6, strokeColor:'#225ea8', strokeOpacity:0.95});
            coolestPolyline.setMap(map);
          }

          const lines = [shortestPolyline, coolestPolyline].filter(Boolean);
          if(lines.length){
            const bounds = new kakao.maps.LatLngBounds();
            lines.forEach(pl => pl.getPath().forEach(ll => bounds.extend(ll)));
            map.setBounds(bounds, 30, 30, 30, 30);
          }

          const fmt = (n)=> (n==null? '-' : (n>=1000? (n/1000).toFixed(2)+' km' : n.toFixed(0)+' m'));
          const pct = (p)=> (p==null? '-' : (p*100).toFixed(1)+'%');
          let html = "";
          if(js.shortest){ html += "최단: "+fmt(js.shortest.total_m)+" / shade "+pct(js.shortest.avg_shade_ratio)+"<br/>"; }
          if(js.coolest){  html += "시원: "+fmt(js.coolest.total_m)+" / shade "+pct(js.coolest.avg_shade_ratio); }
          document.getElementById('stats').innerHTML = html;
        }catch(e){
          alert("오류: "+e.message);
        }finally{
          document.getElementById('run').disabled = false;
          document.getElementById('run').innerText = "실행";
        }
      };

      // ---- Shadow layer loading ----
      function mapBbox(){
        const b = map.getBounds();
        const sw = b.getSouthWest(), ne = b.getNorthEast();
        return [sw.getLng(), sw.getLat(), ne.getLng(), ne.getLat()];
      }
      async function loadShadow(kind){
        const t = document.getElementById('time').value;
        const bbox = mapBbox();
        const qs = new URLSearchParams({bbox: bbox.join(','), time: t || '', kind}).toString();
        const res = await fetch('/shadow?'+qs, {method:'GET'});
        const js  = await res.json();
        if(!res.ok) throw new Error(js.error || 'shadow API 실패');
        return js;
      }
      function clearPolygons(arr){ arr.forEach(p => p.setMap(null)); arr.length = 0; }

      async function refreshShadows(){
        const btn = document.getElementById('refresh-shadow');
        btn.disabled = true; btn.innerText = "불러오는 중...";
        try{
          let stats = [];
          // building
          if(document.getElementById('toggle-building').checked){
            const b = await loadShadow('building');
            clearPolygons(shadowBuilding);
            if(b.gj){ shadowBuilding.push(...polygonsFromGeoJSON(b.gj, {strokeWeight:0.5, strokeColor:'#463f4f', strokeOpacity:1, fillColor:'#28252c', fillOpacity:0.35})); shadowBuilding.forEach(p=>p.setMap(map)); }
            stats.push("건물: "+b.count);
          } else { clearPolygons(shadowBuilding); }
          // tree
          if(document.getElementById('toggle-tree').checked){
            const t = await loadShadow('tree');
            clearPolygons(shadowTree);
            if(t.gj){ shadowTree.push(...polygonsFromGeoJSON(t.gj, {strokeWeight:0.3, strokeColor:'#4daf4a', strokeOpacity:1, fillColor:'#7fc97f', fillOpacity:0.6})); shadowTree.forEach(p=>p.setMap(map)); }
            stats.push("가로수: "+t.count);
          } else { clearPolygons(shadowTree); }
          // shelter
          if(document.getElementById('toggle-shelter').checked){
            const s = await loadShadow('shelter');
            clearPolygons(shadowShelter);
            if(s.gj){ shadowShelter.push(...polygonsFromGeoJSON(s.gj, {strokeWeight:0.3, strokeColor:'#e66101', strokeOpacity:1, fillColor:'#fdae61', fillOpacity:0.6})); shadowShelter.forEach(p=>p.setMap(map)); }
            stats.push("쉼터: "+s.count);
          } else { clearPolygons(shadowShelter); }
          document.getElementById('shadow-stats').innerHTML = stats.join("  /  ");
        }catch(e){
          alert("오류: "+e.message);
        }finally{
          btn.disabled = false; btn.innerText = "갱신";
        }
      }
      document.getElementById('refresh-shadow').onclick = refreshShadows;
      document.getElementById('toggle-building').onchange = refreshShadows;
      document.getElementById('toggle-tree').onchange     = refreshShadows;
      document.getElementById('toggle-shelter').onchange  = refreshShadows;
      kakao.maps.event.addListener(map, 'idle', refreshShadows); // move/zoom 후 자동 갱신
      // first load
      refreshShadows();

      // ---- GeoJSON helpers (Polygon/MultiPolygon/LineString/MultiLineString) ----
      function polygonsFromGeoJSON(gj, style){
        const out = [];
        function latlng(lat, lng){ return new kakao.maps.LatLng(lat, lng); }
        function makePolygon(rings){
          const paths = rings.map(ring => ring.map(([lng,lat]) => latlng(lat,lng)));
          const poly = new kakao.maps.Polygon({
            map:null,
            path: paths,
            strokeWeight: style.strokeWeight ?? 1,
            strokeColor:  style.strokeColor  ?? '#000',
            strokeOpacity:style.strokeOpacity?? 1,
            strokeStyle:  style.strokeStyle  ?? 'solid',
            fillColor:    style.fillColor    ?? '#000',
            fillOpacity:  style.fillOpacity  ?? 0.5
          });
          out.push(poly);
        }
        function walk(geom){
          if(!geom) return;
          const type = geom.type;
          const coords = geom.coordinates;
          if(type==='Polygon'){
            makePolygon(coords);
          }else if(type==='MultiPolygon'){
            coords.forEach(rings => makePolygon(rings));
          }else if(type==='GeometryCollection'){
            (geom.geometries||[]).forEach(g=>walk(g));
          }
        }
        if(gj.type==='FeatureCollection'){
          (gj.features||[]).forEach(f=>walk(f.geometry));
        }else if(gj.type==='Feature'){
          walk(gj.geometry);
        }else{
          walk(gj);
        }
        return out;
      }

      function polylineFromGeoJSON(gj, style){
        const pts = [];
        function latlng(lat, lng){ return new kakao.maps.LatLng(lat, lng); }
        function addLine(line){ line.forEach(([lng,lat])=>pts.push(latlng(lat,lng))); }
        function walk(geom){
          if(!geom) return;
          const type = geom.type;
          const coords = geom.coordinates;
          if(type==='LineString'){
            addLine(coords);
          }else if(type==='MultiLineString'){
            coords.forEach(ls=>addLine(ls));
          }else if(type==='GeometryCollection'){
            (geom.geometries||[]).forEach(g=>walk(g));
          }
        }
        if(gj.type==='FeatureCollection'){
          (gj.features||[]).forEach(f=>walk(f.geometry));
        }else if(gj.type==='Feature'){
          walk(gj.geometry);
        }else{
          walk(gj);
        }
        return new kakao.maps.Polyline({
          path: pts,
          strokeWeight: style.strokeWeight ?? 5,
          strokeColor:  style.strokeColor  ?? '#000',
          strokeOpacity:style.strokeOpacity?? 1,
          strokeStyle:  style.strokeStyle  ?? 'solid'
        });
      }
    }); // kakao.maps.load
  });   // DOMContentLoaded
</script>
</body>
</html>"""

# ---------- Flask routes ----------
app = Flask(__name__)
CORS(app)

@app.get("/")
def index():
    if not KAKAO_JS_KEY:
        return "환경변수 KAKAO_JS_KEY 가 설정되어야 지도가 표시됩니다.", 500
    return render_template_string(MAP_HTML, kakao_js_key=KAKAO_JS_KEY)

@app.get("/route")
def route():
    src_s = request.args.get("src","").strip()
    dst_s = request.args.get("dst","").strip()
    time_s= request.args.get("time","").strip()
    weight= request.args.get("weight", "").strip()

    src = _parse_coord_pair(src_s)
    dst = _parse_coord_pair(dst_s)
    if not src or not dst:
        return jsonify(error="Invalid src/dst. Use 'lat,lon' or 'lon,lat'."), 400

    stamp = _stamp_from_time(time_s)
    union_table = f"shadow_union_{stamp}"
    try:
        cool_weight = float(weight) if weight else DEFAULT_COOL_WEIGHT
    except Exception:
        cool_weight = DEFAULT_COOL_WEIGHT

    try:
        out = _fetch_routes(PG_URL, src, dst, union_table, cool_weight)
        return jsonify(out)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/shadow")
def shadow():
    """Return polygons clipped to bbox for one of: union/building/tree/shelter.
       Query params:
         - kind: union|building|tree|shelter  (default: union)
         - bbox: 'minx,miny,maxx,maxy' EPSG:4326
         - time: 'YYYY-MM-DDTHH:MM' → YYYYMMDD_HHMM
         - tol: simplification tolerance in meters (default 0.7)
    """
    kind   = request.args.get("kind","union").strip().lower()
    bbox_s = request.args.get("bbox","").strip()
    time_s = request.args.get("time","").strip()
    tol_s  = request.args.get("tol","").strip()

    if kind not in {"union","building","tree","shelter"}:
        kind = "union"

    bbox = None
    if bbox_s:
        try:
            parts = [float(x) for x in bbox_s.split(",")]
            if len(parts) == 4:
                bbox = tuple(parts)
        except Exception:
            return jsonify(error="Invalid bbox"), 400

    try:
        tol = float(tol_s) if tol_s else 0.7
    except Exception:
        tol = 0.7

    stamp = _stamp_from_time(time_s)

    prefix = {"union":"shadow_union", "building":"shadow_building", "tree":"shadow_tree", "shelter":"shadow_shelter"}[kind]

    try:
        out = _fetch_shadow_any(PG_URL, prefix, stamp, bbox, simplify_tol_m=tol)
        return jsonify(out)
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == "__main__":
    print(f"Serving on http://{APP_HOST}:{APP_PORT}  (PG_URL={PG_URL})")
    app.run(host=APP_HOST, port=APP_PORT, debug=True)
