# SHADI-map

# Conda 환경 생성 및 활성화
``` py
# 1) Python 3.9 버전 환경 생성
conda create -n shadow_map python=3.9 -y

# 2) 환경 활성화
conda activate shadow_map

# 3) 의존 패키지 설치
conda install -c conda-forge pandas geopandas folium osmnx shapely pyproj numpy pytz pysolar rtree fiona cairo matplotlib -y 
```

# Pgrouting 환경설정 

## 1. 기본 패키지 설치
``` bash
# PostgreSQL, 확장 모듈, PostGIS, OSM → pgRouting 변환 툴
sudo apt-get install -y postgresql postgresql-contrib postgis osm2pgrouting
```
## 2. pgRouting (버전 맞춰 설치)
``` bash
# 설치가능 버전 확인
apt-cache search postgresql-.*-pgrouting | sort

# 예: 16인 경우
sudo apt-get install -y postgresql-16-pgrouting
```
## 3. 서비스/클러스터 시작
``` bash
# 서비스 시작
sudo service postgresql start || true
```

## 4. DB/확장 준비
``` bash
# postgres OS계정으로 접속 테스트
sudo -u postgres psql -c "SELECT version();"

# 비밀번호 설정(원하는 비번으로 변경)
sudo -u postgres psql -c "ALTER USER postgres WITH PASSWORD 'your_password_here';"

# DB 생성
sudo -u postgres createdb shadi

# 확장 설치
sudo -u postgres psql -d shadi -c "CREATE EXTENSION postgis;"
sudo -u postgres psql -d shadi -c "CREATE EXTENSION pgrouting;"

# 확인
sudo -u postgres psql -d shadi -c "\dx"
```

### 확인 결과 
``` bash
                                 List of installed extensions
   Name    | Version |   Schema   |                        Description                         
-----------+---------+------------+------------------------------------------------------------
 pgrouting | 3.6.1   | public     | pgRouting Extension
 plpgsql   | 1.0     | pg_catalog | PL/pgSQL procedural language
 postgis   | 3.4.2   | public     | PostGIS geometry and geography spatial types and functions
(3 rows)
```

# 도로 네트워크 적재 
``` bash
pip install sqlalchemy psycopg2-binary
```

``` bash
python load_yuseong_to_postgis.py
```

# 테이블 생성 - 터미널에 입력
``` bash
sudo -u postgres psql -d shadi
```

``` bash
-- 3-0 geometry 타입을 확실히 LineString으로(혹시 모를 혼합형 대비)
ALTER TABLE ways_raw
  ALTER COLUMN geom TYPE geometry(LineString,4326)
  USING ST_LineMerge(ST_CollectionExtract(geom,2));

-- 3-1 PK 보장
ALTER TABLE ways_raw ADD COLUMN IF NOT EXISTS id bigserial;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ways_raw_pkey') THEN
    ALTER TABLE ways_raw ADD CONSTRAINT ways_raw_pkey PRIMARY KEY (id);
  END IF;
END$$;

-- 3-2 pgRouting 토폴로지
SELECT pgr_createTopology('ways_raw', 0.00001, 'geom', 'id');

-- 3-3 성능 인덱스
CREATE INDEX IF NOT EXISTS ways_raw_geom_gix   ON ways_raw USING GIST (geom);
CREATE INDEX IF NOT EXISTS ways_raw_source_idx ON ways_raw(source);
CREATE INDEX IF NOT EXISTS ways_raw_target_idx ON ways_raw(target);

-- 3-4 기본 길이/코스트
ALTER TABLE ways_raw ADD COLUMN IF NOT EXISTS len_m double precision;
UPDATE ways_raw SET len_m = ST_Length(ST_Transform(geom,5179));

ALTER TABLE ways_raw ADD COLUMN IF NOT EXISTS cost double precision;
ALTER TABLE ways_raw ADD COLUMN IF NOT EXISTS reverse_cost double precision;
UPDATE ways_raw SET cost = len_m, reverse_cost = len_m;
```