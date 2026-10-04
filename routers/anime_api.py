import os
import re
import time
import threading
import requests
import urllib.parse
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup, NavigableString
from typing import List, Dict, Any, Optional, Tuple
from fastapi import APIRouter, Query, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix="/v1", tags=["Anime Downloader"])

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

KUSONIME_BASE = "https://kusonime.com"
KUSONIME_LIST_URL = f"{KUSONIME_BASE}/list-anime-batch-sub-indo/"

# --- Transport hemat: 1 session keep-alive (reuse TLS) untuk semua scrape ---
_SESSION = requests.Session()
_SESSION.headers.update(headers)
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=0)
_SESSION.mount("https://", _adapter)
_SESSION.mount("http://", _adapter)

# --- Cache memori TTL: situs anime jarang berubah, request UI sering berulang ---
_CACHE: Dict[str, Tuple[float, requests.Response]] = {}
_CACHE_LOCK = threading.Lock()


def _cache_lookup(url: str, ttl: int) -> Optional[requests.Response]:
    with _CACHE_LOCK:
        hit = _CACHE.get(url)
        if hit and (time.time() - hit[0]) < ttl:
            return hit[1]
        if hit:
            del _CACHE[url]
    return None


def _cache_store(url: str, resp: requests.Response) -> requests.Response:
    # Simpan salinan ringan (bukan response live) agar aman dipakai ulang.
    cached = requests.Response()
    cached.status_code = resp.status_code
    cached.url = resp.url
    cached.encoding = resp.encoding or "utf-8"
    cached._content = resp.content
    cached.headers["Content-Type"] = resp.headers.get("Content-Type", "text/html")
    with _CACHE_LOCK:
        if len(_CACHE) > 200:  # batasi memori
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[url] = (time.time(), cached)
    return cached


def _fetch(url: str, timeout: Tuple[int, int] = (5, 15), retries: int = 4,
           ttl: int = 0, **kwargs) -> Optional[requests.Response]:
    """GET hemat dengan retry cepat + cache opsional.

    - timeout=(connect, read): gagal-cepat bila jaringan stall,
      karena stall 20 detik adalah penyebab utama lambat.
    - backoff pendek 0.4/0.8/1.2 dtk (dulu 1.5/3/4.5 dtk).
    - ttl>0: kembalikan cache bila masih segar (halaman anime jarang berubah).
    """
    if ttl > 0:
        hit = _cache_lookup(url, ttl)
        if hit is not None:
            return hit
    backoffs = (0.4, 0.8, 1.2)
    for attempt in range(retries):
        try:
            resp = _SESSION.get(url, timeout=timeout,
                                allow_redirects=True, **kwargs)
            resp.raise_for_status()
            if len(resp.text) < 2000 and ("challenge" in resp.text.lower()
                                          or "just a moment" in resp.text.lower()):
                raise ValueError("anti-bot challenge page")
            return _cache_store(url, resp) if ttl > 0 else resp
        except Exception:
            if attempt < retries - 1:
                time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
    return None

class ResolvedFile(BaseModel):
    resolved_url: str
    filename: str
    size: int
    size_formatted: str

class ResolveResponse(BaseModel):
    status: str
    original_url: str
    server: str
    requires_interaction: bool
    files: List[ResolvedFile]

class SearchResult(BaseModel):
    title: str
    url: str

class MirrorLink(BaseModel):
    server: str
    url: str

class PixeldrainFileInfo(BaseModel):
    id: str
    name: str
    size: int
    size_formatted: str
    direct_download_url: str

class PixeldrainFileDetail(BaseModel):
    id: str
    name: str
    size: int
    size_formatted: str
    direct_download_url: str

class PixeldrainListInfo(BaseModel):
    id: str
    title: str
    file_count: int
    files: List[PixeldrainFileDetail]

class DownloadGroup(BaseModel):
    title: str
    links: Dict[str, List[MirrorLink]]

class AnimeDownloadResponse(BaseModel):
    status: str
    search_query: str
    matched_anime: SearchResult
    other_matches: List[SearchResult]
    selected_resolution: str
    selected_server: str
    download_url: str
    pixeldrain_file_info: Optional[PixeldrainFileInfo] = None
    pixeldrain_list_info: Optional[PixeldrainListInfo] = None
    available_links: Dict[str, List[MirrorLink]]
    groups: List[DownloadGroup] = []

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1048576:
        return f"{size_bytes / 1024:.2f} KB"
    else:
        return f"{size_bytes / 1048576:.2f} MB"

def _rank_results(query: str, results: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Urutkan hasil: exact match > awalan > mengandung, lalu abjad."""
    q = query.strip().lower()

    def score(r: Dict[str, str]) -> tuple:
        t = r["title"].strip().lower()
        core = re.sub(r"\s*(bd\s*)?batch\s*subtitle\s*indonesia\s*$", "", t).strip()
        if core == q or t == q:
            return (0, t)
        if core.startswith(q) or t.startswith(q):
            return (1, t)
        if q in core or q in t:
            return (2, t)
        return (3, t)

    return sorted(results, key=score)


def search_anime(query: str) -> List[Dict[str, str]]:
    url = f"{KUSONIME_BASE}/?s={urllib.parse.quote(query)}"
    response = _fetch(url, ttl=1800)
    if response is None:
        return []
    soup = BeautifulSoup(response.text, 'html.parser')
    results = []
    for h2 in soup.find_all('h2'):
        a = h2.find('a')
        if a:
            title = a.get_text().strip()
            href = a.get('href', '').strip()
            if href.startswith(KUSONIME_BASE + "/") and not any(x in href for x in ['/category/', '/genres/', '/list-anime-', '/seasons/', '/seasons-list']):
                results.append({
                    "title": title,
                    "url": href
                })
    # hilangkan duplikat URL, lalu ranking (exact match dulu)
    seen, uniq = set(), []
    for r in results:
        if r["url"] not in seen:
            seen.add(r["url"])
            uniq.append(r)
    return _rank_results(query, uniq)


def scrape_list_page(page: int = 1) -> Dict[str, Any]:
    """Scrape halaman list batch: .daftarkartun > .penzbar > .jdlbar > a.

    Returns: {"entries": [{"title","url"}], "page": int, "total_pages": int}
    """
    url = KUSONIME_LIST_URL if page <= 1 else f"{KUSONIME_LIST_URL}page/{page}/"
    response = _fetch(url, ttl=3600)
    if response is None:
        return {"entries": [], "page": page, "total_pages": 0}

    soup = BeautifulSoup(response.text, 'html.parser')
    container = soup.select_one('.daftarkartun') or soup

    entries: List[Dict[str, str]] = []
    for jdl in container.select('.penzbar .jdlbar'):
        a = jdl.find('a')
        if a and a.get('href'):
            title = a.get_text().strip()
            href = a.get('href', '').strip()
            if title and href.startswith(KUSONIME_BASE + "/"):
                entries.append({"title": title, "url": href})

    # fallback bila struktur berubah
    if not entries:
        seen = set()
        for a in container.find_all('a', href=True):
            href = a['href'].strip()
            title = a.get_text().strip()
            if (href.startswith(KUSONIME_BASE + "/") and len(href) > 30
                    and len(title) > 10 and href not in seen
                    and not any(x in href for x in ["/genres/", "/seasons/",
                               "/page/", "/list-anime-batch"])):
                seen.add(href)
                entries.append({"title": title, "url": href})

    total_pages = 0
    nav = soup.select_one('.wp-pagenavi')
    if nav:
        # Teks nav menempel dengan nomor link, jadi hanya baca text-node langsung.
        direct_text = "".join(
            str(c) for c in nav.children if isinstance(c, NavigableString))
        m = re.search(r'of\s+(\d+)', direct_text)
        if m:
            total_pages = int(m.group(1))
        else:
            nums = [int(x) for a in nav.find_all('a', href=True)
                    for x in re.findall(r'/page/(\d+)/', a['href'])]
            if nums:
                total_pages = max(nums)

    return {"entries": entries, "page": page, "total_pages": total_pages}

def _parse_smokeurl_row(block) -> tuple:
    """Parse satu baris .smokeurlrh -> (RES, [server_links])."""
    strong = block.find('strong')
    res = strong.get_text().strip().upper() if strong else "UNKNOWN"
    server_links = []
    for a in block.find_all('a'):
        server_name = a.get_text().strip()
        url = a.get('href', '').strip()
        if url and server_name:
            server_links.append({"server": server_name, "url": url})
    return res, server_links


def scrape_download_groups(anime_url: str) -> List[Dict[str, Any]]:
    """Scrape per-GRUP part: tiap div.smokeddlrh = 1 kelompok
    (judul di .smokettlrh, baris resolusi di .smokeurlrh).

    Returns: [{"title": str, "downloads": {RES: [server_links]}}]
    """
    response = _fetch(anime_url, ttl=1800)
    if response is None:
        return []
    soup = BeautifulSoup(response.text, 'html.parser')

    groups: List[Dict[str, Any]] = []
    containers = soup.select('div.smokeddlrh')
    if containers:
        for grp in containers:
            title_tag = grp.select_one('.smokettlrh')
            title = title_tag.get_text().strip() if title_tag else ""
            rows = grp.select('div.smokeurlrh') or \
                grp.find_all('div', class_=re.compile(r'smokeurl'))
            downloads_by_res: Dict[str, List[Dict[str, str]]] = {}
            for block in rows:
                res, server_links = _parse_smokeurl_row(block)
                if server_links:
                    downloads_by_res.setdefault(res, []).extend(server_links)
            if downloads_by_res:
                groups.append({"title": title, "downloads": downloads_by_res})

    if not groups:
        flat = scrape_download_links(anime_url)
        if flat:
            groups.append({"title": "", "downloads": flat})
    return groups


def scrape_download_links(anime_url: str) -> Dict[str, List[Dict[str, str]]]:
    # Versi FLAT (gabungan semua part, untuk kompatibilitas):
    # flatten dari scrape_download_groups agar cukup 1x fetch.
    flat: Dict[str, List[Dict[str, str]]] = {}
    for g in scrape_download_groups(anime_url):
        for res, links in g.get("downloads", {}).items():
            flat.setdefault(res, []).extend(links)
    return flat

def get_pixmap_file_info(file_id: str) -> Optional[Dict[str, Any]]:
    info_url = f"https://pixeldrain.com/api/file/{file_id}/info"
    try:
        res = _fetch(info_url, timeout=(5, 10), ttl=3600)
        if res is not None and res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return None

def get_pixmap_list_info(list_id: str) -> Optional[Dict[str, Any]]:
    list_url = f"https://pixeldrain.com/api/list/{list_id}"
    try:
        res = _fetch(list_url, timeout=(5, 10), ttl=3600)
        if res is not None and res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return None

_GDRIVE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def resolve_gdrive_direct_link(file_id: str) -> Dict[str, Any]:
    """Resolve GDrive dengan cache hasil 1 jam (confirm-page jarang berubah)."""
    with _CACHE_LOCK:
        hit = _GDRIVE_CACHE.get(file_id)
        if hit and (time.time() - hit[0]) < 3600:
            return hit[1]
    result = _resolve_gdrive_live(file_id)
    with _CACHE_LOCK:
        _GDRIVE_CACHE[file_id] = (time.time(), result)
    return result


def _resolve_gdrive_live(file_id: str) -> Dict[str, Any]:
    gdrive_base_url = 'https://docs.google.com/uc?export=download'
    for attempt in range(3):
        try:
            response = _SESSION.get(gdrive_base_url, params={'id': file_id},
                                    timeout=(5, 15))
            break
        except Exception:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    else:
        return _gdrive_fallback(file_id)
    try:
        token = None
        for key, value in response.cookies.get_dict().items():
            if key.startswith('download_warning'):
                token = value
                break
        confirm_params = {}
        action_url = 'https://drive.usercontent.google.com/download'
        filename = f"gdrive_file_{file_id}.rar"
        size_bytes = 0
        
        content_type = response.headers.get('content-type', '')
        if 'text/html' in content_type:
            soup = BeautifulSoup(response.text, 'html.parser')
            name_span = soup.find('span', class_='uc-name-size')
            if name_span:
                a_tag = name_span.find('a')
                if a_tag:
                    filename = a_tag.get_text().strip()
                    parent_text = name_span.get_text()
                    size_match = re.search(r'\(([^)]+)\)', parent_text)
                    if size_match:
                        size_str = size_match.group(1)
                        try:
                            num = float(re.findall(r'[0-9.]+', size_str)[0])
                            if 'g' in size_str.lower():
                                size_bytes = int(num * 1024 * 1024 * 1024)
                            elif 'm' in size_str.lower():
                                size_bytes = int(num * 1024 * 1024)
                            elif 'k' in size_str.lower():
                                size_bytes = int(num * 1024)
                        except Exception:
                            pass
            form = soup.find('form', id='download-form')
            if form:
                action_url = form.get('action', action_url)
                for input_tag in form.find_all('input'):
                    name = input_tag.get('name')
                    val = input_tag.get('value')
                    if name and val:
                        confirm_params[name] = val
        
        if confirm_params:
            query_str = urllib.parse.urlencode(confirm_params)
            direct_url = f"{action_url}?{query_str}"
            return {
                "server": "Google Drive",
                "requires_interaction": False,
                "files": [{
                    "resolved_url": direct_url,
                    "filename": filename,
                    "size": size_bytes,
                    "size_formatted": format_size(size_bytes)
                }]
            }
        
        cd = response.headers.get('content-disposition', '')
        if 'filename=' in cd:
            match = re.search(r'filename="([^"]+)"', cd)
            if match:
                filename = match.group(1)
            else:
                filename = cd.split('filename=')[1].split(';')[0].strip()
        try:
            size_bytes = int(response.headers.get('content-length', 0) or 0)
        except (ValueError, TypeError):
            size_bytes = 0
        response.close()
        
        direct_url = f"https://docs.google.com/uc?export=download&id={file_id}"
        if token:
            direct_url += f"&confirm={token}"
        return {
            "server": "Google Drive",
            "requires_interaction": False,
            "files": [{
                "resolved_url": direct_url,
                "filename": filename,
                "size": size_bytes,
                "size_formatted": format_size(size_bytes)
            }]
        }
    except Exception:
        return _gdrive_fallback(file_id)


def _gdrive_fallback(file_id: str) -> Dict[str, Any]:
    direct_url = f"https://docs.google.com/uc?export=download&id={file_id}"
    return {
        "server": "Google Drive",
        "requires_interaction": False,
        "files": [{
            "resolved_url": direct_url,
            "filename": f"gdrive_file_{file_id}.rar",
            "size": 0,
            "size_formatted": "Unknown"
        }]
    }

def resolve_pixeldrain_direct_link(url: str) -> Dict[str, Any]:
    file_match = re.search(r'/u/([a-zA-Z0-9_-]+)', url)
    list_match = re.search(r'/l/([a-zA-Z0-9_-]+)', url)
    files = []
    requires_interaction = False
    
    if file_match:
        file_id = file_match.group(1)
        info = get_pixmap_file_info(file_id)
        if info:
            size_bytes = info.get('size', 0)
            name = info.get('name') or f"{file_id}.rar"
            dl_url = f"https://pixeldrain.com/api/file/{file_id}?download"
            files.append({
                "resolved_url": dl_url,
                "filename": name,
                "size": size_bytes,
                "size_formatted": format_size(size_bytes)
            })
        else:
            dl_url = f"https://pixeldrain.com/api/file/{file_id}?download"
            files.append({
                "resolved_url": dl_url,
                "filename": f"{file_id}.rar",
                "size": 0,
                "size_formatted": "Unknown"
            })
    elif list_match:
        list_id = list_match.group(1)
        list_info = get_pixmap_list_info(list_id)
        if list_info and 'files' in list_info:
            for f in list_info['files']:
                f_id = f.get('id', '')
                f_name = f.get('name', '')
                f_size = f.get('size', 0)
                dl_url = f"https://pixeldrain.com/api/file/{f_id}?download"
                files.append({
                    "resolved_url": dl_url,
                    "filename": f_name,
                    "size": f_size,
                    "size_formatted": format_size(f_size)
                })
        else:
            requires_interaction = True
    else:
        requires_interaction = True
        
    return {
        "server": "Pixeldrain",
        "requires_interaction": requires_interaction,
        "files": files
    }

def get_gdrive_filename_and_size(file_id: str) -> tuple:
    try:
        res = resolve_gdrive_direct_link(file_id)
        if res and res.get('files'):
            f = res['files'][0]
            return f['filename'], f['size']
    except Exception:
        pass
    return f"gdrive_file_{file_id}.rar", 0

# ---------------------------------------------------------------------------
# SAMEHADAKU ONGOING ANIME SCRAPER MODELS & HELPERS
# ---------------------------------------------------------------------------

class OngoingSearchResult(BaseModel):
    title: str
    url: str

class OngoingEpisode(BaseModel):
    title: str
    url: str

class OngoingMirrorLink(BaseModel):
    server: str
    url: str

class OngoingDownloadLinksResponse(BaseModel):
    episode_title: str
    downloads: Dict[str, Dict[str, List[OngoingMirrorLink]]]

class OngoingAnimeDownloadResponse(BaseModel):
    status: str
    episode_title: str
    selected_format: str
    selected_resolution: str
    selected_server: str
    download_url: str
    pixeldrain_file_info: Optional[PixeldrainFileInfo] = None
    pixeldrain_list_info: Optional[PixeldrainListInfo] = None

def search_samehadaku_ongoing(query: Optional[str] = None) -> List[Dict[str, str]]:
    title_query = urllib.parse.quote(query) if query else ""
    url = f"https://v2.samehadaku.how/daftar-anime-2/?title={title_query}&status=Currently+Airing&type=&order=title"
    try:
        response = _fetch(url, ttl=600)
        if response is None:
            return []
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        anime_items = soup.select('.listupd .animepost') or soup.select('.sorlist .numclist') or soup.select('.films .film-item')
        if not anime_items:
            anime_items = soup.find_all('div', class_='animepost') or soup.find_all('div', class_='numclist')
            
        for item in anime_items:
            a_tag = item.find('a')
            title_tag = item.find(class_='title') or item.find('h4') or item.find('h3') or a_tag
            if a_tag and title_tag:
                title = title_tag.get_text().strip()
                href = a_tag.get('href', '').strip()
                if '/anime/' in href:
                    results.append({
                        "title": title,
                        "url": href
                    })
        
        # Fallback if no items found
        if not results:
            links = soup.find_all('a', href=lambda x: x and '/anime/' in x)
            seen = set()
            for l in links:
                href = l.get('href', '').strip()
                if href not in seen:
                    seen.add(href)
                    text = l.get_text().strip()
                    if text:
                        results.append({
                            "title": text,
                            "url": href
                        })
        return results
    except Exception:
        return []

def normalize_samehadaku_url(url: str) -> str:
    url = url.strip()
    if "samehadaku" in url.lower() and "v2.samehadaku.how" not in url.lower():
        # Replace the domain part with v2.samehadaku.how
        url = re.sub(r'https?://[^/]+', 'https://v2.samehadaku.how', url)
    return url

def scrape_samehadaku_episodes(anime_url: str) -> List[Dict[str, str]]:
    anime_url = normalize_samehadaku_url(anime_url)
    try:
        response = _fetch(anime_url, ttl=600)
        if response is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Situs Samehadaku tidak dapat dijangkau saat ini, coba lagi sesaat."
            )
        if response.status_code == 404 or "v2.samehadaku.how" not in response.url.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"URL '{anime_url}' tidak ditemukan atau dialihkan (404). Harap pastikan menggunakan URL anime persis dari output 'daftar_anime_ongoing' tanpa mengubah slug."
            )
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        episode_elements = soup.select('.lstepsiode.listeps ul li')
        if not episode_elements:
            episode_elements = soup.select('.listeps ul li') or soup.select('.lsteps ul li')
            
        for item in episode_elements:
            a_tag = item.select_one('.epsleft .lchx a') or item.select_one('a')
            if a_tag:
                title = a_tag.get_text().strip()
                href = a_tag.get('href', '').strip()
                results.append({
                    "title": title,
                    "url": href
                })
        return results
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal mengambil episode: {str(e)}"
        )

def scrape_samehadaku_download_links(episode_url: str) -> Dict[str, Dict[str, List[Dict[str, str]]]]:
    episode_url = normalize_samehadaku_url(episode_url)
    try:
        response = _fetch(episode_url, ttl=600)
        if response is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Situs Samehadaku tidak dapat dijangkau saat ini, coba lagi sesaat."
            )
        if response.status_code == 404 or "v2.samehadaku.how" not in response.url.lower():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"URL episode '{episode_url}' tidak ditemukan atau dialihkan (404). Harap pastikan menggunakan URL episode persis yang didapatkan dari output 'lihat_episode_ongoing' tanpa mengubah slug."
            )
        soup = BeautifulSoup(response.text, 'html.parser')
        
        download_blocks = soup.select('.download-eps')
        downloads_by_format = {}
        
        for idx, block in enumerate(download_blocks):
            format_title = block.find('p') or block.find('b')
            format_name = format_title.get_text().strip() if format_title else f"Block-{idx}"
            
            # Clean format name (e.g. MKV, MP4, x265)
            if "x265" in format_name.lower():
                format_name = "x265"
            elif "mkv" in format_name.lower():
                format_name = "MKV"
            elif "mp4" in format_name.lower():
                format_name = "MP4"
                
            res_dict = {}
            li_items = block.select('ul li')
            for li in li_items:
                res_tag = li.find('strong')
                res_name = res_tag.get_text().strip() if res_tag else "Unknown"
                
                server_links = []
                for span in li.select('span'):
                    a_tag = span.find('a')
                    if a_tag:
                        server_name = a_tag.get_text().strip()
                        mirror_url = a_tag.get('href', '').strip()
                        if mirror_url:
                            server_links.append({
                                "server": server_name,
                                "url": mirror_url
                            })
                if server_links:
                    res_dict[res_name] = server_links
                    
            if res_dict:
                downloads_by_format[format_name] = res_dict
                
        return downloads_by_format
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal mengambil link unduhan: {str(e)}"
        )

# ---------------------------------------------------------------------------
# SAMEHADAKU ONGOING ANIME ENDPOINTS
# ---------------------------------------------------------------------------

@router.get('/ongoing_anime', response_model=List[OngoingSearchResult])
def ongoing_anime_list(search: Optional[str] = Query(None, description='Cari anime ongoing berdasarkan judul')):
    results = search_samehadaku_ongoing(search)
    return [OngoingSearchResult(title=r['title'], url=r['url']) for r in results]

@router.get('/ongoing_anime/episodes', response_model=List[OngoingEpisode])
def ongoing_anime_episodes(anime_url: str = Query(..., description='URL detail anime dari Samehadaku')):
    anime_url = normalize_samehadaku_url(anime_url)
    results = scrape_samehadaku_episodes(anime_url)
    if not results:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Episode tidak ditemukan atau URL tidak valid."
        )
    return [OngoingEpisode(title=r['title'], url=r['url']) for r in results]

@router.get('/ongoing_anime/download_links', response_model=OngoingDownloadLinksResponse)
def ongoing_anime_download_links(episode_url: str = Query(..., description='URL episode anime dari Samehadaku')):
    episode_url = normalize_samehadaku_url(episode_url)
    episode_title = "Unknown Episode"
    try:
        response = _fetch(episode_url, ttl=600, retries=2)
        if response is not None and response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            h1 = soup.find('h1')
            if h1:
                episode_title = h1.get_text().strip()
    except Exception:
        pass
        
    downloads = scrape_samehadaku_download_links(episode_url)
    if not downloads:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link unduhan tidak ditemukan untuk episode ini."
        )
        
    formatted_downloads = {}
    for fmt, resolutions in downloads.items():
        fmt_dict = {}
        for res, servers in resolutions.items():
            fmt_dict[res] = [OngoingMirrorLink(server=s['server'], url=s['url']) for s in servers]
        formatted_downloads[fmt] = fmt_dict
        
    return OngoingDownloadLinksResponse(
        episode_title=episode_title,
        downloads=formatted_downloads
    )

@router.get('/ongoing_anime/download', response_model=OngoingAnimeDownloadResponse)
def ongoing_anime_download(
    episode_url: str = Query(..., description='URL episode anime dari Samehadaku'),
    format: str = Query(..., description='Format video (misal: MKV, MP4, x265)'),
    resolution: str = Query(..., description='Resolusi video (misal: 480p, 720p, 1080p)'),
    server: Optional[str] = Query(None, description='Server cermin (misal: pixeldrain, gofile, krakenfiles)')
):
    episode_url = normalize_samehadaku_url(episode_url)
    episode_title = "Unknown Episode"
    try:
        response = _fetch(episode_url, ttl=600, retries=2)
        if response is not None and response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            h1 = soup.find('h1')
            if h1:
                episode_title = h1.get_text().strip()
    except Exception:
        pass
        
    downloads = scrape_samehadaku_download_links(episode_url)
    if not downloads:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Link unduhan tidak ditemukan untuk episode ini."
        )
        
    # Match format
    selected_fmt = None
    req_format_clean = format.strip().upper()
    for fmt_key in downloads.keys():
        if req_format_clean in fmt_key.upper():
            selected_fmt = fmt_key
            break
            
    if not selected_fmt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Format '{format}' tidak tersedia. Format yang ada: {', '.join(downloads.keys())}"
        )
        
    resolutions = downloads[selected_fmt]
    selected_res = None
    req_res_clean = resolution.strip().upper()
    for res_key in resolutions.keys():
        if req_res_clean in res_key.upper():
            selected_res = res_key
            break
            
    if not selected_res:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Resolusi '{resolution}' tidak tersedia untuk format '{selected_fmt}'. Resolusi yang ada: {', '.join(resolutions.keys())}"
        )
        
    mirrors = resolutions[selected_res]
    selected_mirror = None
    
    if server:
        req_server_clean = server.strip().lower()
        for mirror in mirrors:
            if req_server_clean in mirror['server'].lower() or req_server_clean in mirror['url'].lower():
                selected_mirror = mirror
                break
        if not selected_mirror:
            available_servers = [m['server'] for m in mirrors]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Server '{server}' tidak tersedia untuk resolusi {selected_res}. Server yang ada: {', '.join(available_servers)}"
            )
    else:
        # Default: prioritize Pixeldrain, then Google Drive, then first available
        for mirror in mirrors:
            if 'pixeldrain' in mirror['url'].lower():
                selected_mirror = mirror
                break
        if not selected_mirror:
            for mirror in mirrors:
                if 'drive.google.com' in mirror['url'].lower() or 'docs.google.com' in mirror['url'].lower():
                    selected_mirror = mirror
                    break
        if not selected_mirror:
            selected_mirror = mirrors[0]
            
    mirror_url = selected_mirror['url']
    is_pixeldrain = 'pixeldrain.com' in mirror_url.lower()
    is_gdrive = 'drive.google.com' in mirror_url.lower() or 'docs.google.com' in mirror_url.lower()
    
    files_to_download = []
    pixeldrain_file_info = None
    pixeldrain_list_info = None
    
    if is_pixeldrain:
        file_match = re.search(r'/u/([a-zA-Z0-9_-]+)', mirror_url)
        list_match = re.search(r'/l/([a-zA-Z0-9_-]+)', mirror_url)
        if file_match:
            file_id = file_match.group(1)
            info = get_pixmap_file_info(file_id)
            if info:
                size_bytes = info.get('size', 0)
                name = info.get('name') or f"{file_id}.rar"
                dl_url = f"https://pixeldrain.com/api/file/{file_id}?download"
                pixeldrain_file_info = PixeldrainFileInfo(
                    id=file_id,
                    name=name,
                    size=size_bytes,
                    size_formatted=format_size(size_bytes),
                    direct_download_url=dl_url
                )
                files_to_download.append((f"pixeldrain_{file_id}", dl_url, name, size_bytes))
        elif list_match:
            list_id = list_match.group(1)
            list_info = get_pixmap_list_info(list_id)
            if list_info and 'files' in list_info:
                files = list_info['files']
                files_clean = []
                for f in files:
                    f_id = f.get('id', '')
                    f_name = f.get('name', '')
                    f_size = f.get('size', 0)
                    dl_url = f"https://pixeldrain.com/api/file/{f_id}?download"
                    files_clean.append(PixeldrainFileDetail(
                        id=f_id,
                        name=f_name,
                        size=f_size,
                        size_formatted=format_size(f_size),
                        direct_download_url=dl_url
                    ))
                    files_to_download.append((f"pixeldrain_{f_id}", dl_url, f_name, f_size))
                pixeldrain_list_info = PixeldrainListInfo(
                    id=list_id,
                    title=list_info.get('title') or 'Unknown List',
                    file_count=len(files),
                    files=files_clean
                )
    elif is_gdrive:
        file_id_match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', mirror_url)
        if not file_id_match:
            file_id_match = re.search(r'/d/([a-zA-Z0-9_-]+)', mirror_url)
        if file_id_match:
            gdrive_id = file_id_match.group(1)
            filename, size_bytes = get_gdrive_filename_and_size(gdrive_id)
            files_to_download.append((f"gdrive_{gdrive_id}", mirror_url, filename, size_bytes))
            
    return OngoingAnimeDownloadResponse(
        status="success",
        episode_title=episode_title,
        selected_format=selected_fmt,
        selected_resolution=selected_res,
        selected_server=selected_mirror['server'],
        download_url=mirror_url,
        pixeldrain_file_info=pixeldrain_file_info,
        pixeldrain_list_info=pixeldrain_list_info
    )

@router.get('/anime_downloader', response_model=AnimeDownloadResponse)
def anime_downloader(
    search: str = Query(..., description='Judul anime yang ingin dicari'),
    resolution: Optional[str] = Query(None, description='Resolusi pilihan (misal: 480p, 720p, 1080p)'),
    server: Optional[str] = Query(None, description='Server cermin download pilihan (misal: pixeldrain, google drive, mega)'),
    anime_url: Optional[str] = Query(None, description='URL detail Kusonime langsung (misal dari other_matches/batch_list) — bila diisi, pencarian dilewati agar part yang dipilih tepat, mis. Part 2')
):
    matched_anime: SearchResult
    other_matches: List[SearchResult] = []
    search_query = search
    if anime_url:
        # Validasi: hanya izinkan URL detail post Kusonime (cegah SSRF ke host lain)
        try:
            host = (urllib.parse.urlparse(anime_url).hostname or "").lower()
        except Exception:
            host = ""
        if host != "kusonime.com":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="anime_url harus URL detail post kusonime.com."
            )
        response = _fetch(anime_url, ttl=1800)
        if response is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Halaman anime tidak dapat dijangkau saat ini, coba lagi sesaat."
            )
        soup = BeautifulSoup(response.text, 'html.parser')
        h1 = soup.select_one('h1.jdlz') or soup.find('h1')
        title = h1.get_text().strip() if h1 else anime_url
        matched_anime = SearchResult(title=title, url=anime_url)
        search_query = f"{search} (URL langsung)"
    else:
        results = search_anime(search)
        if not results:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Anime dengan kata kunci '{search}' tidak ditemukan."
            )
        matched_anime = SearchResult(title=results[0]['title'], url=results[0]['url'])
        other_matches = [SearchResult(title=r['title'], url=r['url']) for r in results[1:]]
    
    # Grup per-part (Part 1, Part 2, ...) untuk tampilan terpisah — 1x fetch.
    # Versi flat (gabungan) dipakai untuk seleksi resolusi/server default.
    _groups_raw = scrape_download_groups(matched_anime.url)
    downloads: Dict[str, List[Dict[str, str]]] = {}
    for g in _groups_raw:
        for res, links in g.get("downloads", {}).items():
            downloads.setdefault(res, []).extend(links)
    if not downloads:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Tidak ada link download batch ditemukan untuk anime ini.'
        )
    if not _groups_raw:
        _groups_raw = [{"title": "", "downloads": downloads}]
    groups_formatted = [
        DownloadGroup(
            title=g.get("title", ""),
            links={
                res_key: [MirrorLink(server=m['server'], url=m['url']) for m in m_list]
                for res_key, m_list in g.get("downloads", {}).items()
            },
        )
        for g in _groups_raw
    ]
        
    selected_res = None
    if resolution:
        req_res_clean = resolution.strip().upper()
        for res_key in downloads.keys():
            if req_res_clean in res_key:
                selected_res = res_key
                break
        if not selected_res:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Resolusi '{resolution}' tidak tersedia. Resolusi yang ada: {', '.join(downloads.keys())}"
            )
    else:
        selected_res = list(downloads.keys())[0]
        
    mirrors = downloads[selected_res]
    selected_mirror = None
    
    if server:
        req_server_clean = server.strip().lower()
        for mirror in mirrors:
            if req_server_clean in mirror['server'].lower() or req_server_clean in mirror['url'].lower():
                selected_mirror = mirror
                break
        if not selected_mirror:
            available_servers = [m['server'] for m in mirrors]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Server '{server}' tidak tersedia untuk resolusi {selected_res}. Server yang ada: {', '.join(available_servers)}"
            )
    else:
        for mirror in mirrors:
            if 'pixeldrain' in mirror['url'].lower():
                selected_mirror = mirror
                break
        if not selected_mirror:
            for mirror in mirrors:
                if 'drive.google.com' in mirror['url'].lower():
                    selected_mirror = mirror
                    break
        if not selected_mirror:
            selected_mirror = mirrors[0]
            
    pixeldrain_file_info = None
    pixeldrain_list_info = None
    mirror_url = selected_mirror['url']
    
    is_pixeldrain = 'pixeldrain.com' in mirror_url.lower()
    is_gdrive = 'drive.google.com' in mirror_url.lower() or 'docs.google.com' in mirror_url.lower()
    
    files_to_download = []
    
    if is_pixeldrain:
        file_match = re.search(r'/u/([a-zA-Z0-9_-]+)', mirror_url)
        list_match = re.search(r'/l/([a-zA-Z0-9_-]+)', mirror_url)
        if file_match:
            file_id = file_match.group(1)
            info = get_pixmap_file_info(file_id)
            if info:
                size_bytes = info.get('size', 0)
                name = info.get('name') or f"{file_id}.rar"
                dl_url = f"https://pixeldrain.com/api/file/{file_id}?download"
                pixeldrain_file_info = PixeldrainFileInfo(
                    id=file_id,
                    name=name,
                    size=size_bytes,
                    size_formatted=format_size(size_bytes),
                    direct_download_url=dl_url
                )
                files_to_download.append((f"pixeldrain_{file_id}", dl_url, name, size_bytes))
        elif list_match:
            list_id = list_match.group(1)
            list_info = get_pixmap_list_info(list_id)
            if list_info and 'files' in list_info:
                files = list_info['files']
                files_clean = []
                for f in files:
                    f_id = f.get('id', '')
                    f_name = f.get('name', '')
                    f_size = f.get('size', 0)
                    dl_url = f"https://pixeldrain.com/api/file/{f_id}?download"
                    files_clean.append(PixeldrainFileDetail(
                        id=f_id,
                        name=f_name,
                        size=f_size,
                        size_formatted=format_size(f_size),
                        direct_download_url=dl_url
                    ))
                    files_to_download.append((f"pixeldrain_{f_id}", dl_url, f_name, f_size))
                pixeldrain_list_info = PixeldrainListInfo(
                    id=list_id,
                    title=list_info.get('title') or 'Unknown List',
                    file_count=len(files),
                    files=files_clean
                )
    elif is_gdrive:
        file_id_match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', mirror_url)
        if not file_id_match:
            file_id_match = re.search(r'/d/([a-zA-Z0-9_-]+)', mirror_url)
        if file_id_match:
            gdrive_id = file_id_match.group(1)
            filename, size_bytes = get_gdrive_filename_and_size(gdrive_id)
            files_to_download.append((f"gdrive_{gdrive_id}", mirror_url, filename, size_bytes))
            
    available_links_formatted = {}
    for res_key, m_list in downloads.items():
        available_links_formatted[res_key] = [
            MirrorLink(server=m['server'], url=m['url']) for m in m_list
        ]
        
    return AnimeDownloadResponse(
        status="success",
        search_query=search_query,
        matched_anime=matched_anime,
        other_matches=other_matches,
        selected_resolution=selected_res,
        selected_server=selected_mirror['server'],
        download_url=mirror_url,
        pixeldrain_file_info=pixeldrain_file_info,
        pixeldrain_list_info=pixeldrain_list_info,
        available_links=available_links_formatted,
        groups=groups_formatted
    )

# Mirror tanpa API direct-link publik (hanya bisa dibuka manual di browser).
MANUAL_MIRRORS = {
    "acefile.co": "Google Sharer / Acefile",
    "mega.nz": "Mega.nz",
    "uptobox.com": "UptoBox",
    "hxfile.co": "Hxfile",
    "lbx.to": "Linkbox",
    "teraboxapp.com": "Terabox",
    "terabox.com": "Terabox",
    "krakenfiles.com": "Krakenfiles",
    "1fichier.com": "1Fichier",
    "mediafire.com": "MediaFire",
    "gofile.io": "Gofile",
}


def manual_mirror_label(url: str) -> Optional[str]:
    lowered = url.lower()
    for domain, label in MANUAL_MIRRORS.items():
        if domain in lowered:
            return label
    return None


class BatchListEntry(BaseModel):
    title: str
    url: str


class BatchListResponse(BaseModel):
    status: str
    page: int
    total_pages: int
    count: int
    entries: List[BatchListEntry]


@router.get('/anime/batch_list', response_model=BatchListResponse)
def anime_batch_list(
    page: int = Query(1, ge=1, description='Nomor halaman (diabaikan bila huruf diisi)'),
    huruf: Optional[str] = Query(None, description='Filter huruf awal judul (A-Z, 0-9) — memindai SEMUA halaman'),
):
    """Jelajahi daftar anime batch Kusonime.

    Tanpa huruf: kembalikan 1 halaman (4 halaman total).
    Dengan huruf: agregat entri berawalan huruf tersebut dari semua halaman.
    """
    if huruf:
        initial = huruf.strip().upper()[:1]
        first = scrape_list_page(1)
        total = first.get("total_pages", 0) or 1
        total = min(total, 10)  # pengaman bila situs berubah
        pages_data = [first]
        if total > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(total - 1, 4)) as pool:
                pages_data.extend(pool.map(scrape_list_page, range(2, total + 1)))
        entries: List[Dict[str, str]] = []
        for pdata in pages_data:
            entries.extend(
                e for e in pdata.get("entries", [])
                if e["title"].lstrip("\u201c\"'(").upper().startswith(initial)
            )
        if not entries:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tidak ada judul berawalan '{initial}' di daftar batch."
            )
        return BatchListResponse(
            status="success",
            page=1,
            total_pages=1,
            count=len(entries),
            entries=[BatchListEntry(title=e["title"], url=e["url"]) for e in entries]
        )

    data = scrape_list_page(page)
    entries = data["entries"]
    total_pages = data["total_pages"]
    if total_pages and page > total_pages:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Halaman {page} melebihi total {total_pages} halaman."
        )
    if not entries:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Daftar batch tidak ditemukan (situs tidak dapat dijangkau)."
        )
    return BatchListResponse(
        status="success",
        page=page,
        total_pages=total_pages,
        count=len(entries),
        entries=[BatchListEntry(title=e["title"], url=e["url"]) for e in entries]
    )

@router.get('/anime_downloader/resolve', response_model=ResolveResponse)
def resolve_anime_url(url: str = Query(..., description='Tautan cermin asal yang ingin di-resolve (Google Drive, Pixeldrain, dll.)')):
    is_pixeldrain = 'pixeldrain.com' in url.lower()
    is_gdrive = 'drive.google.com' in url.lower() or 'docs.google.com' in url.lower() or 'drive.usercontent.google.com' in url.lower()
    
    if is_pixeldrain:
        result = resolve_pixeldrain_direct_link(url)
        files_clean = [
            ResolvedFile(
                resolved_url=f['resolved_url'],
                filename=f['filename'],
                size=f['size'],
                size_formatted=f['size_formatted']
            ) for f in result['files']
        ]
        return ResolveResponse(
            status="success",
            original_url=url,
            server=result['server'],
            requires_interaction=result['requires_interaction'],
            files=files_clean
        )
    elif is_gdrive:
        file_id_match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
        if not file_id_match:
            file_id_match = re.search(r'/d/([a-zA-Z0-9_-]+)', url)
        if not file_id_match:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Tautan Google Drive tidak valid atau ID file tidak dapat ditemukan.'
            )
        file_id = file_id_match.group(1)
        result = resolve_gdrive_direct_link(file_id)
        files_clean = [
            ResolvedFile(
                resolved_url=f['resolved_url'],
                filename=f['filename'],
                size=f['size'],
                size_formatted=f['size_formatted']
            ) for f in result['files']
        ]
        return ResolveResponse(
            status="success",
            original_url=url,
            server=result['server'],
            requires_interaction=result['requires_interaction'],
            files=files_clean
        )
    else:
        label = manual_mirror_label(url) or "External Mirror"
        return ResolveResponse(
            status="success",
            original_url=url,
            server=label,
            requires_interaction=True,
            files=[]
        )

@router.get('/anime_downloader/proxy')
def proxy_download(url: str = Query(..., description='Direct URL to proxy (e.g. Pixeldrain api)')):
    # Strict hostname check (substring matching is bypassable via
    # crafted query strings like https://evil.com/?x=pixeldrain.com).
    try:
        hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        hostname = ""
    if hostname != "pixeldrain.com" and not hostname.endswith(".pixeldrain.com"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Hanya mendukung proxying dari server Pixeldrain."
        )
    
    try:
        response = requests.get(url, headers={"User-Agent": headers["User-Agent"]}, stream=True, timeout=60)
        response.raise_for_status()
        
        headers_to_forward = {}
        if 'Content-Disposition' in response.headers:
            headers_to_forward['Content-Disposition'] = response.headers['Content-Disposition']
        if 'Content-Length' in response.headers:
            headers_to_forward['Content-Length'] = response.headers['Content-Length']
        if 'Content-Type' in response.headers:
            headers_to_forward['Content-Type'] = response.headers['Content-Type']
            
        def iter_content():
            for chunk in response.iter_content(chunk_size=1024*1024):
                if chunk:
                    yield chunk
                    
        return StreamingResponse(iter_content(), headers=headers_to_forward)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Gagal melakukan proxy unduhan: {str(e)}"
        )

