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