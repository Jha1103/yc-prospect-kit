"""Multi-source imports, review-before-save, migration and external website scans."""
import csv
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ycprospect.sources import parse_portfolio_html, parse_csv_rows, safe_web_url
from ycprospect.discovery import EmailFinding, ScanOutcome
from ycprospect.db import connect, upsert_companies, upsert_external, update_prospect
from ycprospect.web import create_server


class ParseTests(unittest.TestCase):
    def test_portfolio_cards_and_nav_omitted(self):
        html='''<html><header><a href="/about">About</a></header><main>
          <article class="portfolio-card"><a href="/portfolio/alpha-ai"><h3>Alpha AI</h3></a><p>Founders: Asha Rao Stage Seed</p></article>
          <article class="startup"><a href="https://beta.io/"><img alt="Beta Labs Logo"></a></article>
          <a href="https://linkedin.com/company/alpha">LinkedIn</a>
          <a href="/privacy">Privacy</a>
          </main></html>'''
        rows=parse_portfolio_html(html,'https://investor.example.com/portfolio')
        self.assertEqual(len(rows),2,rows)
        self.assertEqual(rows[0]['name'],'Alpha AI')
        self.assertEqual(rows[0]['founders'],'Asha Rao')
        self.assertEqual(rows[0]['website'],'')
        self.assertEqual(rows[1]['website'],'https://beta.io/')

    def test_csv_and_url_validation(self):
        values=parse_csv_rows('company,company_url,founder,sector\nOne,https://one.ai,Asha,Fintech\n')
        self.assertEqual(values[0]['name'],'One')
        self.assertEqual(values[0]['founders'],'Asha')
        self.assertEqual(values[0]['industry'],'Fintech')
        for url in ('http://127.0.0.1','file:///tmp/file','https://localhost','http://a.local','https://google.com:8888'):
            with self.assertRaises(ValueError):safe_web_url(url)

    def test_existing_yc_company_receives_vc_attribution(self):
        with tempfile.TemporaryDirectory() as d:
            db=connect(str(Path(d)/'db.sqlite'))
            upsert_companies(db,[{'slug':'one-ai','name':'One AI','website':'https://one.ai/'}])
            slugs=upsert_external(db,[{'name':'One AI','website':'https://www.one.ai/','founders':'Asha Rao'}],
                                  'vc','Elevation','https://elevation.example.com/portfolio')
            self.assertEqual(slugs,['one-ai'])
            types=[r[0] for r in db.execute('SELECT source_type FROM company_sources WHERE slug=?',('one-ai',))]
            self.assertEqual(set(types),{'vc','yc'})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM companies').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT founders FROM companies').fetchone()[0],'Asha Rao')
            update_prospect(db,'one-ai',{'outreach_status':'shortlisted','website':'https://one2.ai/'})
            self.assertEqual(db.execute('SELECT last_scanned FROM companies').fetchone()[0],None)
            db.close()

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.server,_=create_server(port=0,db_path=Path(self.folder.name)/'db.sqlite')
        self.root=f'http://127.0.0.1:{self.server.server_port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3);self.folder.cleanup()
    def get(self,path,payload=None):
        data=json.dumps(payload).encode() if payload is not None else None
        req=Request(self.root+path,data=data,headers={'Content-Type':'application/json'} if data else {})
        with urlopen(req,timeout=4) as r:
            raw=r.read()
            return json.loads(raw) if 'application/json' in r.headers['Content-Type'] else raw
    def test_preview_and_import_scan_export(self):
        fixture='''<html><article class="portfolio-item"><a href="https://flux.ai/">Flux AI</a></article>
                     <article class="portfolio-item"><a href="/portfolio/sigma">Sigma Ops</a></article></html>'''
        with patch('ycprospect.sources._robots_allowed',return_value=True), patch('ycprospect.sources._get_html',return_value=(fixture,'https://vc.example.com/portfolio')):
            result=self.get('/api/source/preview',{'url':'https://vc.example.com/portfolio'})
            self.assertEqual(result['count'],2)
        inserted=self.get('/api/source/import',{'source_type':'vc','source_name':'Example Ventures',
            'source_url':'https://vc.example.com/portfolio','items':result['items']})
        self.assertEqual(inserted['imported'],2)
        saved=self.get('/api/companies?scope=last')['items']
        self.assertEqual(len(saved),2)
        flux=next(x for x in saved if x['name']=='Flux AI')
        self.assertEqual(flux['backed_by'],'Example Ventures')
        self.assertEqual(self.get('/api/stats')['external_companies'],2)
        self.assertEqual(self.get('/api/facets')['sources'],['Example Ventures'])
        self.assertTrue(self.get('/api/prospect/update',{'slug':flux['slug'],'changes':{'outreach_status':'shortlisted','founders':'Asha Rao'}})['saved'])
        self.assertEqual(self.get('/api/companies?outreach_status=shortlisted')['total'],1)
        with patch('ycprospect.web.scan_company_contacts',return_value=ScanOutcome(
            contacts=[EmailFinding('hello@flux.ai','company_contact','https://flux.ai/contact')],checked=2,result='found')):
            self.get('/api/source/scan',{'max_pages':1,'rescan_days':0})
            deadline=time.monotonic()+5
            while time.monotonic()<deadline and self.get('/api/job')['status']=='running':time.sleep(.03)
        self.assertEqual(self.get('/api/job')['status'],'complete')
        records=list(csv.DictReader(io.StringIO(self.get('/api/export?type=companies').decode('utf-8-sig'))))
        self.assertEqual(len(records),2)
        self.assertEqual(next(x for x in records if x['name']=='Flux AI')['founders'],'Asha Rao')
        self.assertEqual(len(self.get('/api/export?type=contacts').decode().splitlines()),2)
    def test_csv_import_and_rejection(self):
        self.get('/api/source/csv',{'csv':'company,website,founders\nAtlas,https://atlas.ai,Asha Rao',
             'source_type':'accelerator','source_name':'Antler'})
        self.assertEqual(self.get('/api/companies?source_type=accelerator')['total'],1)
        self.assertIn(b'founder_links',self.get('/api/template.csv'))
        with self.assertRaises(HTTPError) as error:
            self.get('/api/source/import',{'source_type':'vc','source_name':'Bad','items':[{'name':'Bad','website':'http://localhost'}]})
        self.assertEqual(error.exception.code,400)
        self.assertEqual(self.get('/api/stats')['companies'],1)

if __name__=='__main__':unittest.main()
