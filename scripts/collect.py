"""Radyo Gunes V9.2: conservative public event collector (stdlib only)."""
import datetime as dt
import hashlib
import html
import json
import re
import urllib.request
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
        super().__init__(); self.links=[]; self.link=None; self.text=[]; self.scripts=[]; self.script=False; self.buf=[]; self.meta={}; self.title=''; self.in_title=False
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
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
    def handle_endtag(self,tag):
        if tag=='a' and self.link is not None: self.links.append((self.link,clean(' '.join(self.text)))); self.link=None; self.text=[]
        if tag=='script' and self.script: self.scripts.append(''.join(self.buf)); self.script=False
        if tag=='title': self.in_title=False

def fetch(url):
    req=urllib.request.Request(url,headers={'User-Agent':'RadyoGunesEvents/1.2 (+https://radyogunes.com)','Accept':'text/html'})
    with urllib.request.urlopen(req,timeout=18) as r:
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

def make(title,start,url,source,category='',venue='',district=''):
    title=clean(title)
    if not title or len(title)<6 or not start or not url.startswith('https://'):return None
    now=dt.datetime.now(TZ)
    if not now-dt.timedelta(days=1)<=start<=now+dt.timedelta(days=365):return None
    key=hashlib.sha256((url.lower()+'|'+start.date().isoformat()).encode()).hexdigest()[:20]
    return {'id':key,'title':title[:180],'start':start.isoformat(),'venue':clean(venue)[:120], 'city':'Bursa','district':district,'category':category or 'Kültür & Sanat','url':url,'source':source}

def collect():
    events=[];seen=set()
    def add(e):
        if e and e['id'] not in seen:events.append(e);seen.add(e['id']);return True
        return False
    for source in SOURCES:
        try:p=fetch(source)
        except Exception as exc:print('Source skipped:',source,type(exc).__name__,str(exc)[:160]);continue
        accepted=0; candidates=0
        for obj in jsonld_events(p):
            title=clean(obj.get('name'));start=parse_iso(obj.get('startDate'))
            loc=obj.get('location') or {};loc=loc[0] if isinstance(loc,list) and loc else loc;loc=loc if isinstance(loc,dict) else {}
            addr=loc.get('address') or {};addr=addr if isinstance(addr,dict) else {}
            url=obj.get('url') or source
            if not isinstance(url,str) or urlparse(url).netloc!=urlparse(source).netloc:url=source
            e=make(title,start,url,source,clean(obj.get('eventType')),loc.get('name'),clean(addr.get('addressLocality')))
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
                try:
                    detail=fetch(url)
                    og=clean(detail.meta.get('og:title'))
                    if og and len(og)>len(title) and 'inegöl belediyesi' not in og.lower().split('|')[-1]:title=og.split('|')[0].strip()
                except Exception as exc:print('Detail unavailable:',url,type(exc).__name__)
                if add(make(title,start,url,source,category,district='İnegöl')):accepted+=1
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
                if d and d>=dt.datetime.now(TZ)-dt.timedelta(days=1) and e.get('id') not in existing and e.get('url','').startswith('https://'):
                    events.append(e);existing.add(e['id'])
        except (ValueError,TypeError,KeyError):pass
    events.sort(key=lambda x:x['start'])
    out.write_text(json.dumps({'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'events':events},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Saved',len(events),'events')
