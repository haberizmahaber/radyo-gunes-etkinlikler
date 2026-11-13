"""Conservative JSON-LD Event collector. Python stdlib; no paid API."""
import datetime as dt, hashlib, html, json, os, re, urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

SOURCES=[
 'https://www.bursa.bel.tr/etkinlik',
 'https://www.inegol.bel.tr/etkinlikler',
 'https://www.bubilet.com.tr/bursa',
]
class Scripts(HTMLParser):
 def __init__(self):super().__init__();self.capture=False;self.buf=[];self.items=[]
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='script' and 'ld+json' in a.get('type','').lower():self.capture=True;self.buf=[]
 def handle_data(self,data):
  if self.capture:self.buf.append(data)
 def handle_endtag(self,tag):
  if tag=='script' and self.capture:self.items.append(''.join(self.buf));self.capture=False

def walk(obj):
 if isinstance(obj,list):
  for v in obj:yield from walk(v)
 elif isinstance(obj,dict):
  typ=obj.get('@type',[]);typ=[typ] if isinstance(typ,str) else typ
  if any(str(t).endswith('Event') for t in typ):yield obj
  for v in obj.values():
   if isinstance(v,(dict,list)):yield from walk(v)

def clean(v):return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]+>',' ',str(v or '')))).strip()
def valid_url(s):return isinstance(s,str) and urlparse(s).scheme=='https' and bool(urlparse(s).netloc)
def parse_date(s):
 if not isinstance(s,str):return None
 try:
  x=dt.datetime.fromisoformat(s.replace('Z','+00:00'))
  if x.tzinfo is None:x=x.replace(tzinfo=dt.timezone(dt.timedelta(hours=3)))
  return x
 except ValueError:return None

def collect():
 result=[];seen=set();now=dt.datetime.now(dt.timezone.utc)
 for source in SOURCES:
  try:
   req=urllib.request.Request(source,headers={'User-Agent':'RadyoGunesEvents/1.0 (+https://radyogunes.com; public event metadata)','Accept':'text/html'})
   with urllib.request.urlopen(req,timeout=18) as r:
    if r.status!=200:continue
    body=r.read(2_000_000).decode('utf-8','replace')
   p=Scripts();p.feed(body)
   for raw in p.items:
    try:objects=list(walk(json.loads(raw)))
    except (ValueError,TypeError):continue
    for o in objects:
     title=clean(o.get('name'));start=parse_date(o.get('startDate'))
     if not title or not start or start<now-dt.timedelta(days=1) or start>now+dt.timedelta(days=365):continue
     loc=o.get('location',{});loc=loc[0] if isinstance(loc,list) and loc else loc;loc=loc if isinstance(loc,dict) else {}
     addr=loc.get('address',{});addr=addr if isinstance(addr,dict) else {}
     place=' '.join(clean(v) for v in (loc.get('name'),addr.get('addressLocality'),addr.get('addressRegion'))).lower()
     if not any(k in place for k in ('bursa','inegöl','inegol','nilüfer','nilufer','osmangazi','yıldırım','yildirim','mudanya','gemlik','gürsu','gursu','kestel')):continue
     link=o.get('url') or source
     if not valid_url(link):link=source
     key=hashlib.sha256((title.lower()+'|'+start.date().isoformat()+'|'+clean(loc.get('name')).lower()).encode()).hexdigest()[:20]
     if key in seen:continue
     seen.add(key)
     result.append({'id':key,'title':title[:180],'start':start.isoformat(),'venue':clean(loc.get('name'))[:120], 'city':clean(addr.get('addressRegion') or 'Bursa')[:60], 'district':clean(addr.get('addressLocality'))[:60], 'category':clean(o.get('eventType') or 'Kültür & Sanat')[:60], 'url':link,'source':source})
  except Exception as exc:print('Source skipped:',source,type(exc).__name__,str(exc)[:160])
 return sorted(result,key=lambda e:e['start'])

if __name__=='__main__':
 out=Path('data/events.json');out.parent.mkdir(parents=True,exist_ok=True)
 events=collect()
 # Keep last known future events if sources temporarily fail; no stale past dates.
 if not events and out.exists():
  try:
   old=json.loads(out.read_text(encoding='utf-8'))
   now=dt.datetime.now(dt.timezone.utc)
   events=[x for x in old.get('events',[]) if (parse_date(x.get('start')) or now-dt.timedelta(days=2))>=now-dt.timedelta(days=1)]
  except (ValueError,TypeError):pass
 out.write_text(json.dumps({'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'events':events},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
 print('Saved',len(events),'events')
