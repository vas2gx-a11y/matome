#!/usr/bin/env python3
"""自分専用まとめアンテナ生成スクリプト（標準ライブラリのみ・追加インストール不要）

feeds.txt のブログからRSSを集め、新しい順に並べた docs/index.html を作ります。
"""
import concurrent.futures as cf
import html
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
DATA = DOCS / "items.json"
JST = timezone(timedelta(hours=9))
KEEP_DAYS = 3          # 何日分の記事を残すか
MAX_ITEMS = 600        # 最大表示件数
UA = "Mozilla/5.0 (personal RSS reader; matome-antenna)"

# ---------- 設定ファイル読み込み ----------
def read_lines(path):
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]

def load_feeds():
    feeds = []
    for line in read_lines(ROOT / "feeds.txt"):
        if "|" in line:
            name, url = [x.strip() for x in line.split("|", 1)]
        else:
            name, url = "", line
        feeds.append((name, url))
    return feeds

# ---------- 取得 ----------
def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.geturl()

def looks_like_feed(data):
    head = data[:2000].lower()
    return b"<rss" in head or b"<rdf" in head or b"<feed" in head

def discover(url):
    """URLがRSSならそのまま、HTMLならRSSを自動で探す"""
    data, final = fetch(url)
    if looks_like_feed(data):
        return data
    text = data.decode("utf-8", "ignore")
    for m in re.finditer(r"<link[^>]+>", text, re.I):
        tag = m.group(0)
        if re.search(r"application/(rss|atom|rdf)\+xml", tag, re.I):
            href = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if href:
                d, _ = fetch(urljoin(final, html.unescape(href.group(1))))
                if looks_like_feed(d):
                    return d
    base = final if final.endswith("/") else final + "/"
    for cand in ("index.rdf", "atom.xml", "feed", "rss", "index.xml"):
        try:
            d, _ = fetch(urljoin(base, cand))
            if looks_like_feed(d):
                return d
        except Exception:
            pass
    raise ValueError("RSSが見つかりません")

# ---------- 解析 ----------
def local(tag):
    return tag.rsplit("}", 1)[-1].lower()

def child_text(el, *names):
    for c in el:
        if local(c.tag) in names and (c.text or "").strip():
            return c.text.strip()
    return ""

def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=JST)
    return d

def find_image(el):
    for c in el.iter():
        n = local(c.tag)
        if n in ("thumbnail", "content") and c.get("url", "").startswith("http"):
            if n == "thumbnail" or "image" in c.get("type", "image"):
                return c.get("url")
        if n == "enclosure" and "image" in c.get("type", ""):
            return c.get("url")
    for c in el.iter():
        if local(c.tag) in ("encoded", "description", "content", "summary") and c.text:
            m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)', c.text, re.I)
            if m:
                return html.unescape(m.group(1))
    return ""

def parse_feed(data, blog_name):
    root = ET.fromstring(data)
    if not blog_name:
        for el in root.iter():
            if local(el.tag) in ("channel", "feed"):
                blog_name = child_text(el, "title")
                break
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        title = child_text(el, "title")
        link = child_text(el, "link")
        if not link:
            for c in el:
                if local(c.tag) == "link" and c.get("href") and c.get("rel", "alternate") == "alternate":
                    link = c.get("href")
                    break
        if not link and el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"):
            link = el.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
        date = parse_date(child_text(el, "pubdate", "date", "published", "updated"))
        if not (title and link and date):
            continue
        # PR・広告記事を除外
        if re.match(r"^\s*(PR|AD|【PR】|\[PR\])[:：\s]", title):
            continue
        items.append({
            "title": html.unescape(title),
            "link": link,
            "blog": blog_name,
            "ts": int(date.timestamp()),
            "img": find_image(el),
        })
    return items

def get_one(feed):
    name, url = feed
    try:
        return parse_feed(discover(url), name), None
    except Exception as e:
        return [], f"{name or url}: {e}"

# ---------- 重複まとめ ----------
def norm(title):
    t = re.sub(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)", "", title)
    t = re.sub(r"[\s!！?？。、・…「」『』〜～ｗw]+", "", t)
    return t[:25]

def main():
    feeds = load_feeds()
    ng = read_lines(ROOT / "ng_words.txt")

    old = []
    if DATA.exists():
        try:
            old = json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            pass

    new, errors = [], []
    with cf.ThreadPoolExecutor(8) as ex:
        for items, err in ex.map(get_one, feeds):
            new += items
            if err:
                errors.append(err)

    cutoff = int((datetime.now(JST) - timedelta(days=KEEP_DAYS)).timestamp())
    now = int(datetime.now(JST).timestamp()) + 600
    by_link = {i["link"]: i for i in old + new}   # 新しい取得結果で上書き
    items = [i for i in by_link.values() if cutoff <= i["ts"] <= now]
    items = [i for i in items if not any(w in i["title"] for w in ng)]
    items.sort(key=lambda i: i["ts"], reverse=True)

    # 同じネタ（タイトルがほぼ同じ）は最初の1件にまとめ、他ブログ分を「他N件」に
    merged, seen = [], {}
    for i in items:
        key = norm(i["title"])
        if key and key in seen:
            seen[key].setdefault("dups", []).append({"blog": i["blog"], "link": i["link"]})
            continue
        i = dict(i)
        i.pop("dups", None)
        seen[key] = i
        merged.append(i)
    merged = merged[:MAX_ITEMS]

    DOCS.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    page = TEMPLATE.replace("/*DATA*/", json.dumps(merged, ensure_ascii=False).replace("</", "<\\/"))
    page = page.replace("{{UPDATED}}", datetime.now(JST).strftime("%m/%d %H:%M"))
    page = page.replace("{{ERRORS}}", html.escape(" / ".join(errors)))
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    print(f"{len(merged)}件 出力（取得{len(new)}件, エラー{len(errors)}件）")
    for e in errors:
        print("  ⚠", e)

TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="まとめ">
<title>まとめアンテナ</title>
<style>
:root{--bg:#f4f4f6;--card:#fff;--text:#1c1c1e;--sub:#8e8e93;--line:#e5e5ea;--accent:#0a84ff;--read:#aeaeb2;--star:#ff9f0a}
@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f2f2f7;--sub:#8e8e93;--line:#2c2c2e;--read:#636366}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Hiragino Sans",sans-serif;padding-bottom:calc(env(safe-area-inset-bottom) + 60px)}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:calc(env(safe-area-inset-top) + 8px) 12px 8px;border-bottom:1px solid var(--line)}
.top{display:flex;align-items:center;gap:8px}
h1{font-size:18px;margin:0;flex:1}
.upd{font-size:11px;color:var(--sub)}
input{width:100%;margin-top:8px;padding:9px 12px;border:none;border-radius:10px;background:var(--card);color:var(--text);font-size:16px}
ul{list-style:none;margin:0;padding:0}
li{background:var(--card);border-bottom:1px solid var(--line)}
.row{display:flex;align-items:flex-start}
a.m{flex:1;min-width:0;display:flex;gap:10px;padding:11px 0 11px 12px;color:inherit;text-decoration:none}
.thumb{flex:none;width:64px;height:64px;border-radius:8px;object-fit:cover;background:var(--line)}
.t{font-size:15px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
li.unread .t{font-weight:600}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent);margin-right:6px;vertical-align:middle}
li.read .t{color:var(--read)}
.meta{font-size:11px;color:var(--sub);margin-top:4px}
.star{flex:none;border:none;background:none;font-size:22px;line-height:1;padding:12px;color:var(--line)}
.star.on{color:var(--star)}
.dups{padding:0 12px 9px 86px;font-size:11px}
.dups a{color:var(--accent);margin-right:8px;text-decoration:none}
.err{font-size:11px;color:#ff453a;padding:8px 12px}
.err:empty{display:none}
.btn{border:none;background:none;color:var(--accent);font-size:13px;padding:4px}
.empty{padding:40px 12px;text-align:center;color:var(--sub);font-size:14px}
#more{display:block;width:100%;padding:16px;border:none;background:none;color:var(--accent);font-size:15px}
nav{position:fixed;left:0;right:0;bottom:0;z-index:5;display:flex;background:var(--card);border-top:1px solid var(--line);padding-bottom:env(safe-area-inset-bottom)}
nav button{flex:1;border:none;background:none;padding:10px 0;font-size:13px;color:var(--sub)}
nav button.on{color:var(--accent);font-weight:600}
</style></head><body>
<header>
 <div class="top"><h1 id="ttl">最新</h1><span class="upd">更新 {{UPDATED}}</span>
 <button class="btn" id="allRead">全部既読</button></div>
 <input id="q" type="search" placeholder="キーワードで絞り込み">
</header>
<div class="err">{{ERRORS}}</div>
<ul id="list"></ul>
<button id="more">もっと見る</button>
<nav>
 <button data-tab="latest">最新</button>
 <button data-tab="unread">未読</button>
 <button data-tab="bm">★ ブックマーク</button>
</nav>
<script>
const ITEMS=/*DATA*/;
const store={get(k,d){try{return JSON.parse(localStorage.getItem(k))??d}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
let read=new Set(store.get("read",[])),bm=store.get("bm",{}),tab=store.get("tab","latest"),shown=60;
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const saveRead=()=>store.set("read",[...read].slice(-3000));
function ago(ts){const m=Math.floor((Date.now()/1000-ts)/60);if(m<60)return Math.max(m,0)+"分前";if(m<1440)return Math.floor(m/60)+"時間前";const d=new Date(ts*1000);return (d.getMonth()+1)+"/"+d.getDate()}
function source(){
 if(tab==="bm")return Object.values(bm).sort((a,b)=>b.saved-a.saved);
 if(tab==="unread")return ITEMS.filter(i=>!read.has(i.link));
 return ITEMS}
function render(){
 const q=$("q").value.trim();
 const list=source().filter(i=>!q||i.title.includes(q));
 const unread=ITEMS.filter(i=>!read.has(i.link)).length;
 document.querySelectorAll("nav button").forEach(b=>{b.classList.toggle("on",b.dataset.tab===tab);
  if(b.dataset.tab==="unread")b.textContent="未読"+(unread?`（${unread}）`:"")});
 $("ttl").textContent={latest:"最新",unread:"未読",bm:"ブックマーク"}[tab];
 $("allRead").style.visibility=tab==="bm"?"hidden":"visible";
 $("list").innerHTML=list.length?list.slice(0,shown).map(i=>{const r=read.has(i.link);return `<li class="${r?"read":"unread"}"><div class="row"><a class="m" href="${esc(i.link)}" target="_blank" rel="noopener" data-l="${esc(i.link)}">
 ${i.img?`<img class="thumb" loading="lazy" referrerpolicy="no-referrer" src="${esc(i.img)}" onerror="this.style.visibility='hidden'">`:`<div class="thumb"></div>`}
 <div><div class="t">${r?"":'<span class="dot"></span>'}${esc(i.title)}</div><div class="meta">${esc(i.blog)}・${ago(i.ts)}</div></div></a>
 <button class="star${bm[i.link]?" on":""}" data-l="${esc(i.link)}" aria-label="ブックマーク">★</button></div>
 ${i.dups?`<div class="dups">他でも: ${i.dups.map(d=>`<a href="${esc(d.link)}" target="_blank" rel="noopener">${esc(d.blog)}</a>`).join("")}</div>`:""}</li>`}).join("")
 :`<div class="empty">${tab==="bm"?"★を押すとここに保存されます":tab==="unread"?"未読の記事はありません":"記事がありません"}</div>`;
 $("more").style.display=list.length>shown?"block":"none";
 document.querySelectorAll("a.m").forEach(a=>a.onclick=()=>{read.add(a.dataset.l);saveRead();setTimeout(render,300)});
 document.querySelectorAll(".star").forEach(b=>b.onclick=()=>{const l=b.dataset.l;
  if(bm[l])delete bm[l];else{const it=ITEMS.find(i=>i.link===l);if(it){const{dups,...x}=it;bm[l]={...x,saved:Date.now()}}}
  store.set("bm",bm);render()})}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{tab=b.dataset.tab;store.set("tab",tab);shown=60;window.scrollTo(0,0);render()});
$("q").oninput=()=>{shown=60;render()};
$("more").onclick=()=>{shown+=60;render()};
$("allRead").onclick=()=>{if(!confirm("表示中の記事をすべて既読にしますか？"))return;ITEMS.forEach(i=>read.add(i.link));saveRead();render()};
render();
</script></body></html>"""

if __name__ == "__main__":
    main()
