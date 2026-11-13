"""Radyo Gunes V9.2: conservative public event collector (stdlib only)."""
import datetime as dt
import hashlib
import html
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

TZ = dt.timezone(dt.timedelta(hours=3))
SOURCES = ['https://www.bursa.bel.tr/etkinlik', 'https://www.inegol.bel.tr/etkinlikler']
MONTHS = {'oca':1,'şub':2,'sub':2,'mar':3,'nis':4,'may':5,'haz':6,'tem':7,'ağu':8,'agu':8,'eyl':9,'eki':10,'kas':11,'ara':12}
CATEGORIES = ['tiyatro','konser','festival','sergi','atölye','atolye','söyleşi','soylesi','gösteri','gosteri','sinema','çocuk','cocuk']

def clean(s):
    return re.sub(r'\s+', ' ', html.unescape(str(s or ''))).strip()

class Parser(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.link=None; self.text=[]; self.scripts=[]; self.script=False; self.buf=[]; self.meta={}; self.title=''; self.in_title=False; self.images=[]; self.stack=[]; self.cards=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in ('article','li','div'):
            cls=a.get('class','').lower()
            if any(t in cls for t in ('event','etkinlik','card','item')):
                self.stack.append({'tag':tag,'depth':0,'parts':[],'links':[],'images':[]})
        if tag not in ('img','meta','input','br','hr','link','source','area','embed','wbr'):
            for card in self.stack: card['depth']+=1
        if tag=='img':
            src=a.get('data-src') or a.get('data-original') or a.get('src') or ''
            if src:
                self.images.append((src,clean(a.get('alt',''))))
                for card in self.stack:card['images'].append((src,clean(a.get('alt',''))))
        if tag=='a' and a.get('href'):
            for card in self.stack:card['links'].append(a['href'])
        if tag=='a' and a.get('href') and self.link is None: self.link=a['href']; self.text=[]
        if tag=='script' and 'ld+json' in a.get('type','').lower(): self.script=True; self.buf=[]
        if tag=='meta':
            key=a.get('property') or a.get('name')
            if key and a.get('content'): self.meta[key.lower()]=a['content']
        if tag=='title': self.in_title=True
    def handle_data(self, data):
        if self.link is not None: self.text.append(data)
        if self.script: self.buf.append(data)
        if self.in_title: self.title+=data
        for card in self.stack:card['parts'].append(data)
    def handle_endtag(self,tag):
        if tag=='a' and self.link is not None: self.links.append((self.link,clean(' '.join(self.text)))); self.link=None; self.text=[]
        if tag=='script' and self.script: self.scripts.append(''.join(self.buf)); self.script=False
        if tag=='title': self.in_title=False
        for card in self.stack:card['depth']-=1
        finished=[card for card in self.stack if card['depth']<=0]
        for card in finished:
            card['text']=clean(' '.join(card['parts']))
            self.cards.append(card);self.stack.remove(card)

_DETAIL_CACHE = {}

def fetch(url):
    if url in _DETAIL_CACHE: return _DETAIL_CACHE[url]
    req=urllib.request.Request(url,headers={'User-Agent':'RadyoGunesEvents/1.2 (+https://radyogunes.com)','Accept':'text/html'})
    with urllib.request.urlopen(req,timeout=10) as r:
        if r.status!=200: raise ValueError('HTTP '+str(r.status))
        body=r.read(2_000_000).decode('utf-8','replace')
    p=Parser();p.feed(body);return p

def parse_iso(value):
    try:
        d=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return d.replace(tzinfo=TZ) if d.tzinfo is None else d
    except (ValueError,TypeError): return None

def listing_date(text):
    # Example: 25 KAS '26 20.00 Tiyatro ...
    m=re.search(r'\b(\d{1,2})\s+(OCA|ŞUB|SUB|MAR|NİS|NIS|MAY|HAZ|TEM|AĞU|AGU|EYL|EKİ|EKI|KAS|ARA)\s*[’\']?\s*(20\d{2}|\d{2})\s+(\d{1,2})[.:](\d{2})\b',text,re.I)
    if not m:return None,None
    mon=MONTHS.get(m.group(2).lower().replace('ı','i'))
    if not mon:return None,None
    year=int(m.group(3));year=year+2000 if year<100 else year
    try: return dt.datetime(year,mon,int(m.group(1)),int(m.group(4)),int(m.group(5)),tzinfo=TZ),m.end()
    except ValueError:return None,None

def jsonld_events(p):
    def walk(obj):
        if isinstance(obj,list):
            for x in obj:yield from walk(x)
        elif isinstance(obj,dict):
            types=obj.get('@type',[]);types=[types] if isinstance(types,str) else types
            if any(str(t).endswith('Event') for t in types):yield obj
            for x in obj.values():
                if isinstance(x,(list,dict)):yield from walk(x)
    for raw in p.scripts:
        try:yield from walk(json.loads(raw))
        except (ValueError,TypeError):pass

def image_url(candidate,base):
    if not isinstance(candidate,str) or not candidate:return ''
    u=urljoin(base,candidate.strip())
    parsed=urlparse(u)
    if parsed.scheme!='https' or parsed.hostname not in ('www.bursa.bel.tr','bursa.bel.tr','www.inegol.bel.tr','inegol.bel.tr') or parsed.username or parsed.password:return ''
    if re.search(r'\.(svg|gif)(?:[?#]|$)',parsed.path,re.I):return ''
    return u

def best_image(p,url):
    for key in ('og:image','twitter:image'):
        found=image_url(p.meta.get(key,''),url)
        if found:return found
    # İnegöl detaylarında OG görseli yoksa yalnızca etkinlikle ilişkili görselleri dene.
    for src,alt in p.images:
        candidate=image_url(src,url)
        if not candidate:continue
        if re.search(r'logo|icon|avatar|banner|social|placeholder',src+' '+alt,re.I):continue
        if '/etkinlik/' in urlparse(candidate).path.lower() or re.search(r'tiyatro|konser|etkinlik|afiş|afis',alt,re.I):
            return candidate
    return ''

def inegol_event_image(p,url):
    """Use only event-specific imagery; never reuse municipality-wide OG banners."""
    if not p:return ''
    for src,alt in p.images:
        candidate=image_url(src,url)
        if not candidate:continue
        path=urlparse(candidate).path.lower()
        if re.search(r'logo|icon|avatar|banner|social|placeholder|default|belediye',src+' '+alt,re.I):continue
        if re.search(r'\.(?:jpe?g|png|webp)$',path) and (re.search(r'afiş|afis|poster|tiyatro|konser',alt,re.I) or re.search(r'/etkinlik(?:ler)?/|/upload/',path,re.I)):
            return candidate
    return ''

def bursa_date(s):
    # Listing dates: 10.11.2026 - 17.11.2026 or 14.11.2026
    m=re.search(r'(?<!\d)(\d{2})\.(\d{2})\.(20\d{2})(?:\s*[-–]\s*(\d{2})\.(\d{2})\.(20\d{2}))?',s)
    if not m:return None,None
    try:
        start=dt.datetime(int(m[3]),int(m[2]),int(m[1]),12,0,tzinfo=TZ)
        end=dt.datetime(int(m[6]),int(m[5]),int(m[4]),23,59,tzinfo=TZ) if m[6] else start
        return start,end
    except ValueError:return None,None

def make(title,start,url,source,category='',venue='',district='',image='',end=None):
    title=clean(title)
    if not title or len(title)<6 or not start or not url.startswith('https://'):return None
    now=dt.datetime.now(TZ)
    if not now-dt.timedelta(days=1)<=start<=now+dt.timedelta(days=365) and not (end and now<=end<=now+dt.timedelta(days=365)):return None
    key=hashlib.sha256((url.lower()+'|'+start.date().isoformat()).encode()).hexdigest()[:20]
    return {'id':key,'title':title[:180],'start':start.isoformat(),'venue':clean(venue)[:120], 'city':'Bursa','district':district,'category':category or 'Kültür & Sanat','url':url,'source':source,'image':image_url(image,url),'end':end.isoformat() if end else ''}

def collect():
    events=[];seen=set()
    def add(e):
        if e and e['id'] not in seen:events.append(e);seen.add(e['id']);return True
        return False
    for source in SOURCES:
        try:p=fetch(source)
        except Exception as exc:print('Source skipped:',source,type(exc).__name__,str(exc)[:160]);continue
        accepted=0; candidates=0
        # Preload detail pages concurrently; a slow page cannot block all other pages.
        urls=[]
        for href,label in p.links:
            u=urljoin(source,href); parsed=urlparse(u)
            if parsed.hostname not in ('www.bursa.bel.tr','bursa.bel.tr','www.inegol.bel.tr','inegol.bel.tr') or not parsed.path.startswith('/etkinlik/'):continue
            if 'inegol.bel.tr' in source and not listing_date(label)[0]:continue
            if u not in urls:urls.append(u)
        if 'bursa.bel.tr' in source:
            card_urls=[]
            for card in p.cards:
                start,end=bursa_date(card.get('text',''))
                if not start or (end or start)<dt.datetime.now(TZ)-dt.timedelta(days=1):continue
                for href in card.get('links',[]):
                    u=urljoin(source,href)
                    if urlparse(u).hostname in ('www.bursa.bel.tr','bursa.bel.tr') and urlparse(u).path.startswith('/etkinlik/'):
                        card_urls.append(u);break
            urls=list(dict.fromkeys(card_urls))
        urls=urls[:50]
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures={pool.submit(fetch,u):u for u in urls}
            for future in as_completed(futures):
                try:_DETAIL_CACHE[futures[future]]=future.result()
                except Exception:pass
        print(f'Prefetched {len(_DETAIL_CACHE)} detail pages for {source}',flush=True)
        for obj in jsonld_events(p):
            title=clean(obj.get('name'));start=parse_iso(obj.get('startDate'))
            loc=obj.get('location') or {};loc=loc[0] if isinstance(loc,list) and loc else loc;loc=loc if isinstance(loc,dict) else {}
            addr=loc.get('address') or {};addr=addr if isinstance(addr,dict) else {}
            url=obj.get('url') or source
            if not isinstance(url,str) or urlparse(url).netloc!=urlparse(source).netloc:url=source
            e=make(title,start,url,source,clean(obj.get('eventType')),loc.get('name'),clean(addr.get('addressLocality')),image=(obj.get('image',[None])[0] if isinstance(obj.get('image'),list) and obj.get('image') else obj.get('image','')) if isinstance(obj.get('image'),(str,list)) else '')
            if add(e):accepted+=1
        # Inegol municipality: detail URL and dated listing label (not news or navigation)
        if 'inegol.bel.tr' in source:
            for href,label in p.links:
                url=urljoin(source,href);parsed=urlparse(url)
                if parsed.netloc not in ('www.inegol.bel.tr','inegol.bel.tr') or not parsed.path.startswith('/etkinlik/'):continue
                candidates+=1
                start,pos=listing_date(label)
                if not start:continue
                remainder=label[pos:].strip()
                category='Kültür & Sanat'
                for word in CATEGORIES:
                    if remainder.lower().startswith(word):category=word.capitalize();remainder=remainder[len(word):].strip();break
                title=remainder
                # The listing can truncate labels. Use detail page OG title when available.
                detail=None
                try:
                    detail=_DETAIL_CACHE.get(url)
                    og=clean(detail.meta.get('og:title')) if detail else '' 
                    if og and len(og)>len(title) and 'inegöl belediyesi' not in og.lower().split('|')[-1]:title=og.split('|')[0].strip()
                except Exception as exc:print('Detail unavailable:',url,type(exc).__name__)
                if add(make(title,start,url,source,category,district='İnegöl',image=inegol_event_image(detail,url) if detail else '')):accepted+=1
        if 'bursa.bel.tr' in source:
            for card in p.cards:
                start,end=bursa_date(card.get('text',''))
                if not start or (end or start)<dt.datetime.now(TZ)-dt.timedelta(days=1):continue
                urls=[urljoin(source,h) for h in card.get('links',[]) if urlparse(urljoin(source,h)).path.startswith('/etkinlik/')]
                urls=[u for u in urls if urlparse(u).hostname in ('www.bursa.bel.tr','bursa.bel.tr')]
                if not urls:continue
                url=urls[0]
                if url.rstrip('/')==source.rstrip('/'):continue
                candidates+=1
                detail=_DETAIL_CACHE.get(url)
                if detail is None:continue
                title=clean(detail.meta.get('og:title') or detail.title).split('|')[0].strip()
                title=re.sub(r'\s*[-–]\s*Bursa Büyükşehir Belediyesi.*$','',title,flags=re.I).strip()
                if not title or title.lower() in ('etkinlikler','etkinlik'):continue
                category='Kültür & Sanat'
                for c in ('Konser','Sergi','Atölye','Panel-Söyleşi','Festival','Tiyatro','Konferans'):
                    if re.search(r'(?<!\w)'+re.escape(c)+r'(?!\w)',card['text'],re.I):category=c;break
                venue=''
                m=re.search(r'\bYer\s*:\s*([^|]+)',card['text'],re.I)
                if m:venue=clean(m[1])[:120]
                image=best_image(detail,url)
                if not image:
                    for src,alt in card['images']:
                        if src:image=image_url(src,source);break
                if add(make(title,start,url,source,category,venue,image=image,end=end)):accepted+=1
        print(f'Source: {source} | detail candidates={candidates} | accepted={accepted}')
    print('Collected',len(events),'events before fallback')
    return sorted(events,key=lambda x:x['start'])

if __name__=='__main__':
    out=Path('data/events.json');out.parent.mkdir(parents=True,exist_ok=True)
    events=collect()
    if out.exists():
        try:
            old=json.loads(out.read_text(encoding='utf-8'))
            existing={e['id'] for e in events}
            for e in old.get('events',[]):
                d=parse_iso(e.get('start'))
                if d and (d>=dt.datetime.now(TZ)-dt.timedelta(days=1) or (parse_iso(e.get('end')) and parse_iso(e.get('end'))>=dt.datetime.now(TZ))) and e.get('id') not in existing and e.get('url','').startswith('https://'):
                    events.append(e);existing.add(e['id'])
        except (ValueError,TypeError,KeyError):pass
    # A generic İnegöl banner must not appear as the poster for several events.
    image_counts={}
    for e in events:
        if e.get('district')=='İnegöl' and e.get('image'):
            image_counts[e['image']]=image_counts.get(e['image'],0)+1
    for e in events:
        if e.get('district')=='İnegöl' and e.get('image') and image_counts[e['image']]>1:
            e['image']=''
    events.sort(key=lambda x:x['start'])
    out.write_text(json.dumps({'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'events':events},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Saved',len(events),'events')
