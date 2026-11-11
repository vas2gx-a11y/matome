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
ARCH = DOCS / "archive"     # 日付別の保存（archive/2026-11-11.json）
ARCHIVE_DAYS = 90           # 何日分さかのぼれるようにするか
SITES = DOCS / "sites.json"   # サイトごとの最終更新（取得失敗時は前回の値を使う）
JST = timezone(timedelta(hours=9))
KEEP_DAYS = 3          # 何日分の記事を残すか
MAX_ITEMS = 1000       # 最大表示件数
UA = "Mozilla/5.0 (personal RSS reader; matome-antenna)"

# ---------- 設定ファイル読み込み ----------
def read_lines(path):
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]

DEFAULT_CAT = "総合・雑談"
CATS = {}   # 表示名 → カテゴリー（feeds.txt の3列目）

def load_feeds():
    feeds = []
    for line in read_lines(ROOT / "feeds.txt"):
        parts = [x.strip() for x in line.split("|")]
        if len(parts) == 1:
            parts = ["", parts[0]]
        name, url = parts[0], parts[1]
        if name:
            CATS[name] = (parts[2] if len(parts) > 2 and parts[2] else DEFAULT_CAT)
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

# ---------- 日付別アーカイブ ----------
def update_archive(items):
    """記事を公開日（日本時間）ごとのファイルに追記し、古い日付は削除。日付一覧を返す"""
    ARCH.mkdir(parents=True, exist_ok=True)
    by_day = {}
    for i in items:
        d = datetime.fromtimestamp(i["ts"], JST).strftime("%Y-%m-%d")
        by_day.setdefault(d, []).append({k: i[k] for k in ("title", "link", "blog", "ts", "img") if k in i})
    for d, its in by_day.items():
        f = ARCH / f"{d}.json"
        cur = {}
        if f.exists():
            try:
                cur = {x["link"]: x for x in json.loads(f.read_text(encoding="utf-8"))}
            except Exception:
                pass
        cur.update({x["link"]: x for x in its})
        text = json.dumps(sorted(cur.values(), key=lambda x: x["ts"], reverse=True), ensure_ascii=False)
        if not f.exists() or f.read_text(encoding="utf-8") != text:
            f.write_text(text, encoding="utf-8")
    oldest = (datetime.now(JST) - timedelta(days=ARCHIVE_DAYS)).strftime("%Y-%m-%d")
    days = []
    for f in sorted(ARCH.glob("*.json"), reverse=True):
        if f.stem < oldest:
            f.unlink()
            continue
        try:
            days.append({"d": f.stem, "n": len(json.loads(f.read_text(encoding="utf-8")))})
        except Exception:
            pass
    return days

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
    prev_sites = {}
    if SITES.exists():
        try:
            prev_sites = {x["name"]: x for x in json.loads(SITES.read_text(encoding="utf-8"))}
        except Exception:
            pass
    sites = []
    with cf.ThreadPoolExecutor(8) as ex:
        for (name, url), (items, err) in zip(feeds, ex.map(get_one, feeds)):
            new += items
            if err:
                errors.append(err)
            label = name or (items[0]["blog"] if items else url)
            last = max((i["ts"] for i in items), default=None) or prev_sites.get(label, {}).get("last")
            sites.append({"name": label, "url": url, "cat": CATS.get(name, DEFAULT_CAT),
                          "last": last, "err": bool(err)})

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
    days = update_archive(items)
    SITES.write_text(json.dumps(sites, ensure_ascii=False), encoding="utf-8")
    DATA.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    page = TEMPLATE.replace("/*DATA*/", json.dumps(merged, ensure_ascii=False).replace("</", "<\\/"))
    cat_order = list(dict.fromkeys(list(CATS.values()) + [DEFAULT_CAT]))
    page = page.replace("/*CATS*/", json.dumps({"map": CATS, "order": cat_order}, ensure_ascii=False).replace("</", "<\\/"))
    page = page.replace("/*DAYS*/", json.dumps(days))
    page = page.replace("/*SITES*/", json.dumps(sites, ensure_ascii=False).replace("</", "<\\/"))
    page = page.replace("{{UPDATED}}", datetime.now(JST).strftime("%m/%d %H:%M"))
    page = page.replace("{{ERRORS}}", html.escape(" / ".join(errors)))
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    print(f"{len(merged)}件 出力（取得{len(new)}件, エラー{len(errors)}件）")
    for e in errors:
        print("  ⚠", e)

TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="まとめ">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="theme-color" content="#f4f4f6" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<link rel="manifest" href="manifest.webmanifest">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<title>まとめアンテナ</title>
<style>
:root{--bg:#f4f4f6;--card:#fff;--text:#1c1c1e;--sub:#8e8e93;--line:#e5e5ea;--accent:#0a84ff;--read:#aeaeb2;--star:#ff9f0a}
@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f2f2f7;--sub:#8e8e93;--line:#2c2c2e;--read:#636366}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,"Hiragino Sans",sans-serif;padding-bottom:calc(env(safe-area-inset-bottom) + 60px)}
header{position:sticky;top:0;z-index:5;background:var(--bg);padding:calc(env(safe-area-inset-top) + 8px) 12px 8px;border-bottom:1px solid var(--line)}
.top{display:flex;align-items:center;gap:8px}
h1{font-size:18px;margin:0;flex:1}
.upd{font-size:11px;color:var(--sub)}
button.upd{color:var(--sub);font-size:11px}
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
.why{color:var(--accent)}
.hint{font-size:12px;color:var(--sub);padding:8px 12px;background:var(--card);border-bottom:1px solid var(--line)}
.chips{display:none;gap:6px;overflow-x:auto;padding:8px 12px;background:var(--bg);border-bottom:1px solid var(--line);scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chips button{flex:none;border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:16px;padding:6px 12px;font-size:13px}
.chips button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.site{display:flex;justify-content:space-between;gap:8px;padding:11px 12px;background:var(--card);border-bottom:1px solid var(--line);font-size:14px}
.site .last{flex:none;font-size:12px;color:var(--sub)}
.site.stale .last{color:#ff453a}
.cathead{padding:14px 12px 6px;font-size:12px;font-weight:600;color:var(--sub)}
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
 <div class="top"><h1 id="ttl">最新</h1><button class="upd btn" id="reload" aria-label="再読み込み">更新 {{UPDATED}} ↻</button>
 <button class="btn" id="sitesBtn">登録サイト</button></div>
 <input id="q" type="search" placeholder="キーワードで絞り込み">
</header>
<div class="err">{{ERRORS}}</div>
<div class="hint" id="hint"></div>
<div class="chips" id="chips"></div>
<div class="chips" id="days"></div>
<ul id="list"></ul>
<button id="more">もっと見る</button>
<nav>
 <button data-tab="latest">最新</button>
 <button data-tab="rec">おすすめ</button>
 <button data-tab="cat">カテゴリー</button>
 <button data-tab="bm">★ ブックマーク</button>
</nav>
<script>
const ITEMS=/*DATA*/;
const CATS=/*CATS*/;
const SITES=/*SITES*/;
const DAYS=/*DAYS*/;   // [{d:"2026-11-11",n:件数}, ...] 新しい順
let day="",dayData={};  // day=""は「すべて（直近3日）」
function loadDay(d){if(dayData[d])return;dayData[d]="loading";
 fetch("archive/"+d+".json").then(r=>r.json()).then(x=>{dayData[d]=x;render()}).catch(()=>{dayData[d]=[];render()})}
function dayLabel(d){const t=new Date(),y=new Date(Date.now()-86400000),f=x=>`${x.getFullYear()}-${String(x.getMonth()+1).padStart(2,"0")}-${String(x.getDate()).padStart(2,"0")}`;
 if(d===f(t))return"今日";if(d===f(y))return"昨日";const x=new Date(d+"T00:00:00");return `${x.getMonth()+1}/${x.getDate()}(${"日月火水木金土"[x.getDay()]})`}
const catOf=i=>CATS.map[i.blog]||"総合・雑談";
const store={get(k,d){try{return JSON.parse(localStorage.getItem(k))??d}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
let read=new Set(store.get("read",[])),bm=store.get("bm",{}),tab=store.get("tab","latest"),shown=60,cat=store.get("cat",CATS.order[0]);
if(!CATS.order.includes(cat))cat=CATS.order[0];
if(tab==="unread")tab="latest";  // 廃止した未読タブを選んでいた人向け
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
// 記事は同じタブで開く（戻るでアンテナに戻る）
const href=l=>`href="${esc(l)}"`;
const saveRead=()=>store.set("read",[...read].slice(-3000));
// ---- おすすめ：自分が読んだ記事（ブログ・キーワード）の傾向で並べる ----
let hist=store.get("hist",[]);  // 開いた・★した記事の {b:ブログ, t:タイトル}
const STOP=new Set(["悲報","朗報","速報","画像","動画","衝撃","疑問","議論","海外の反応","ワイ","ｗｗｗ","これ","それ","あれ","さん","ちゃん","まとめ","話題","結果"]);
function words(t){t=t.replace(/【[^】]*】|\[[^\]]*\]/g," ");
 return [...new Set((t.match(/[一-龠々]{2,}|[ァ-ヶー]{2,}|[A-Za-zＡ-Ｚａ-ｚ0-9０-９]{3,}/g)||[]).filter(w=>!STOP.has(w)))]}
function addHist(it,w=1){if(!it)return;for(let k=0;k<w;k++)hist.push({b:it.blog,t:it.title});hist=hist.slice(-600);store.set("hist",hist)}
function recommend(){
 const bc={},wc={};hist.forEach(h=>{bc[h.b]=(bc[h.b]||0)+1;words(h.t).forEach(w=>wc[w]=(wc[w]||0)+1)});
 const n=hist.length,nb=Object.keys(bc).length||1,now=Date.now()/1000;
 return ITEMS.filter(i=>now-i.ts<48*3600).map(i=>{
  const blog=(bc[i.blog]||0)/n*nb;              // そのブログをどれだけ多く読むか（平均=1）
  const hits=words(i.title).filter(w=>wc[w]>=2).sort((a,b)=>wc[b]-wc[a]);
  const kw=hits.reduce((s,w)=>s+Math.log(1+wc[w]),0);
  const score=(blog*1.5+kw+(i.dups?i.dups.length*0.5:0))*Math.exp(-(now-i.ts)/(36*3600));
  const why=hits.length?"よく読む話題: "+hits.slice(0,2).join("・"):blog>1.3?"よく読むブログ":"";
  return {...i,score,why}}).sort((a,b)=>b.score-a.score)}
function ago(ts){const m=Math.floor((Date.now()/1000-ts)/60);if(m<60)return Math.max(m,0)+"分前";if(m<1440)return Math.floor(m/60)+"時間前";const d=new Date(ts*1000);return (d.getMonth()+1)+"/"+d.getDate()}
function source(){
 if(tab==="bm")return Object.values(bm).sort((a,b)=>b.saved-a.saved);
 if(tab==="cat")return ITEMS.filter(i=>catOf(i)===cat);
 if(tab==="rec")return hist.length<10?ITEMS:recommend();
 if(day){loadDay(day);return Array.isArray(dayData[day])?dayData[day]:[]}
 return ITEMS}
function render(){
 const q=$("q").value.trim();
 const list=source().filter(i=>!q||i.title.includes(q));
 document.querySelectorAll("nav button").forEach(b=>{b.classList.toggle("on",b.dataset.tab===tab)});
 $("ttl").textContent={latest:"最新",rec:"おすすめ",cat:"カテゴリー",bm:"ブックマーク",sites:"登録サイト"}[tab];
 $("q").style.display=$("more").style.display=tab==="sites"?"none":"";
 if(tab==="sites"){$("hint").style.display="block";$("hint").textContent=`${SITES.length}サイト登録中。最終更新が7日以上前のサイトは赤で表示します`;
  $("chips").style.display="none";$("days").style.display="none";
  const fmt=ts=>{if(!ts)return"記事なし";const d=new Date(ts*1000);return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,"0")}:${String(d.getMinutes()).padStart(2,"0")}`};
  $("list").innerHTML=CATS.order.map(c=>{const ss=SITES.filter(x=>x.cat===c);if(!ss.length)return"";
   return `<div class="cathead">${esc(c)}</div>`+ss.map(x=>{const stale=!x.last||Date.now()/1000-x.last>7*86400;
    return `<div class="site${stale?" stale":""}"><span>${esc(x.name)}</span><span class="last">${x.err?"取得失敗・":""}${fmt(x.last)}</span></div>`}).join("")}).join("");
  document.querySelectorAll("nav button").forEach(b=>b.classList.remove("on"));return}
 $("days").style.display=tab==="latest"?"flex":"none";
 $("days").innerHTML=`<button class="${day?"":"on"}" data-d="">すべて</button>`+DAYS.map(x=>`<button class="${x.d===day?"on":""}" data-d="${x.d}">${dayLabel(x.d)}（${x.n}）</button>`).join("");
 document.querySelectorAll("#days button").forEach(b=>b.onclick=()=>{day=b.dataset.d;shown=60;window.scrollTo(0,0);render()});
 $("chips").style.display=tab==="cat"?"flex":"none";
 $("chips").innerHTML=CATS.order.map(c=>`<button class="${c===cat?"on":""}" data-c="${esc(c)}">${esc(c)}（${ITEMS.filter(i=>catOf(i)===c).length}）</button>`).join("");
 document.querySelectorAll("#chips button").forEach(b=>b.onclick=()=>{cat=b.dataset.c;store.set("cat",cat);shown=60;window.scrollTo(0,0);render()});
 $("hint").style.display=tab==="rec"?"block":"none";
 $("hint").textContent=hist.length<10?`記事を読むほど好みに合わせて並びます（あと${10-hist.length}件読むと開始）`:"よく読むブログ・話題が近い順（直近48時間）";
 $("list").innerHTML=list.length?list.slice(0,shown).map(i=>{const r=read.has(i.link);return `<li class="${r?"read":"unread"}"><div class="row"><a class="m" ${href(i.link)} data-l="${esc(i.link)}">
 ${i.img?`<img class="thumb" loading="lazy" referrerpolicy="no-referrer" src="${esc(i.img)}" onerror="this.style.visibility='hidden'">`:`<div class="thumb"></div>`}
 <div><div class="t">${r?"":'<span class="dot"></span>'}${esc(i.title)}</div><div class="meta">${esc(i.blog)}・${ago(i.ts)}${i.why?` <span class="why">${esc(i.why)}</span>`:""}</div></div></a>
 <button class="star${bm[i.link]?" on":""}" data-l="${esc(i.link)}" aria-label="ブックマーク">★</button></div>
 ${i.dups?`<div class="dups">他でも: ${i.dups.map(d=>`<a ${href(d.link)}>${esc(d.blog)}</a>`).join("")}</div>`:""}</li>`}).join("")
 :`<div class="empty">${tab==="bm"?"★を押すとここに保存されます":tab==="latest"&&day&&dayData[day]==="loading"?"読み込み中…":"記事がありません"}</div>`;
 $("more").style.display=list.length>shown?"block":"none";
 document.querySelectorAll("a.m").forEach(a=>a.onclick=()=>{if(!read.has(a.dataset.l))addHist(ITEMS.find(i=>i.link===a.dataset.l));read.add(a.dataset.l);saveRead();setTimeout(render,300)});
 document.querySelectorAll(".star").forEach(b=>b.onclick=()=>{const l=b.dataset.l;
  if(bm[l])delete bm[l];else{const it=ITEMS.find(i=>i.link===l);if(it){const{dups,score,why,...x}=it;bm[l]={...x,saved:Date.now()};addHist(it,2)}}
  store.set("bm",bm);render()})}
document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{tab=b.dataset.tab;store.set("tab",tab);shown=60;window.scrollTo(0,0);render()});
$("sitesBtn").onclick=()=>{tab="sites";window.scrollTo(0,0);render()};
$("q").oninput=()=>{shown=60;render()};
$("more").onclick=()=>{shown+=60;render()};
$("reload").onclick=()=>location.reload();
// アプリ版は再読み込みボタンがないので、15分以上離れてから戻ったら自動で最新にする
let hiddenAt=0;
document.addEventListener("visibilitychange",()=>{if(document.hidden)hiddenAt=Date.now();else if(hiddenAt&&Date.now()-hiddenAt>15*60*1000)location.reload()});
// 同じタブで記事を開いて「戻る」したとき、既読を反映し直す
window.addEventListener("pageshow",e=>{if(e.persisted){read=new Set(store.get("read",[]));bm=store.get("bm",{});hist=store.get("hist",[]);render()}});
if("serviceWorker" in navigator)navigator.serviceWorker.register("sw.js").catch(()=>{});
render();
</script></body></html>"""

if __name__ == "__main__":
    main()
