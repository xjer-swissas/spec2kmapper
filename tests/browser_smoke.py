"""Run with PLAYWRIGHT_BROWSERS_PATH set after installing Playwright Chromium.
Uses mocked API responses to exercise the actual Alpine UI without customer data.
"""
import base64
import json
import os
import sys
from pathlib import Path
import time
import requests
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ASSETS = Path('/tmp/spec2500-browser-assets')
ASSETS.mkdir(exist_ok=True)
for name, url in [('alpine.js', 'https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js'),
                  ('tailwind.js', 'https://cdn.tailwindcss.com')]:
    if not (ASSETS / name).exists():
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        (ASSETS / name).write_text(response.text)

payload = base64.urlsafe_b64encode(json.dumps({'exp': int(time.time()) + 3600}).encode()).decode().rstrip('=')
token = 'header.' + payload + '.signature'
root_match = dict(candidate='ENGINE-1', score=100, match_type='EXACT', description='Engine')
child_match = dict(candidate='CHILD-1', score=70, match_type='SEMANTIC_DESCRIPTION', description='Oil pump')
state = {'expired': False, 'sync': 'SUCCESS', 'candidate_calls': 0, 'source': 'database', 'amoscentral_address': ''}


def route_request(route):
    path = route.request.url.split('spec2500.test')[-1].split('?')[0]
    if path == '/':
        return route.fulfill(body=(ROOT / 'app/static/index.html').read_text(), content_type='text/html')
    if path == '/app.js':
        return route.fulfill(body=(ROOT / 'app/static/app.js').read_text(), content_type='application/javascript')
    if state['expired'] and path != '/api/auth/login':
        return route.fulfill(status=401, json={'detail': 'Could not validate credentials'})
    if path == '/api/auth/login':
        state['expired'] = False
        data = dict(access_token=token, schema_name='tenant_test')
    elif path == '/api/auth/me':
        data = {'schema_name': 'tenant_test'}
    elif path == '/api/amos/matching-settings':
        data = dict(ranking_mode='weighted', reranker='none', collect_confirmed_mappings=False, tuning_enabled=False, pn_weight=0.25, is_admin=True, confirmed_count=0)
        if route.request.method == 'PUT':
            data.update(route.request.post_data_json)
    elif path == '/api/amos/status':
        data = dict(status=state['sync'], phase='Loading documents' if state['sync'] == 'RUNNING' else 'Complete',
                    counts=dict(parts=100, relationships=20, positions=10, documents=5), last_success_at='2026-09-07T10:00:00')
    elif path == '/api/amos/config':
        if route.request.method == 'POST':
            state.update({k: route.request.post_data_json[k] for k in ['source', 'amoscentral_address']})
        data = dict(host='amos', port=5432, dbname='amos', user='user', password='', refresh_interval_hours=24,
                    source=state['source'], amoscentral_address=state['amoscentral_address'])
    elif path == '/api/amos/csv/queries':
        from app.amos.queries import EXPORTS
        data = EXPORTS
    elif path == '/api/amos/csv/import':
        assert 'name="parts"' in route.request.post_data
        assert 'name="configurations"' in route.request.post_data
        assert 'name="documents"' in route.request.post_data
        data = {'message': 'CSV import complete. Automatic database refresh is disabled.'}
    elif path == '/api/amos/sync':
        state['sync'] = 'RUNNING'; data = {'status': 'RUNNING'}
    elif path == '/api/amos/part-search':
        data = dict(items=[dict(part_number='OTHER-9', description='Alternate oil pump', match_type='SUGGESTION', pn_score=None, semantic_score=89, description_score=50, in_configuration=False)], index={'status': 'READY'}, semantic_used=True)
    elif path == '/api/amos/cache':
        data = dict(items=[dict(part_number='OTHER-9', description='Alternate oil pump')],
                    columns=['part_number', 'description'], total=1)
    elif path == '/api/spec2500/upload':
        data = dict(session_id='session', detected_root_pn='ENGINE-1', root_matches=[root_match], root_description='Engine')
    elif path == '/api/spec2500/candidates/session':
        state['candidate_calls'] += 1
        data = dict(pn_matches={'SOURCE-1': [child_match] + [dict(child_match, candidate=f'CHILD-{i}') for i in range(2, 51)]}, fp_matches={}, doc_matches={},
                    pn_descriptions={'SOURCE-1': 'Lubrication pump'}, available_pns_pool=['CHILD-1'], pn_children_map={})
    elif path == '/api/spec2500/export':
        return route.fulfill(body='<Crate/>', content_type='application/xml')
    else:
        raise AssertionError(path)
    route.fulfill(json=data)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'],
                                executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'))
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('http://spec2500.test/**', route_request)
    page.route('https://cdn.jsdelivr.net/**', lambda route: route.fulfill(body=(ASSETS / 'alpine.js').read_text(), content_type='application/javascript'))
    page.route('https://cdn.tailwindcss.com/**', lambda route: route.fulfill(body=(ASSETS / 'tailwind.js').read_text(), content_type='application/javascript'))
    page.route('https://cdnjs.cloudflare.com/**', lambda route: route.fulfill(body='', content_type='text/css'))
    page.goto('http://spec2500.test/')
    page.locator('input[x-model="authForm.username"]').fill('tester')
    page.locator('input[x-model="authForm.password"]').fill('password')
    page.get_by_role('button', name='Sign In', exact=True).click()
    expect(page.get_by_role('button', name='Start over', exact=True)).not_to_be_visible()
    page.get_by_role('button', name='AMOS DB', exact=True).click()
    expect(page.get_by_text('Alternate oil pump', exact=True)).to_be_visible()
    page.get_by_role('button', name='SQL / CSV import', exact=True).click()
    assert 'SELECT DISTINCT' in page.get_by_label('parts SQL query').input_value()
    import_button = page.get_by_role('button', name='Import CSV files and replace cache')
    expect(import_button).to_be_disabled()
    for kind in ['parts', 'configurations', 'documents']:
        page.get_by_label('Upload ' + kind + ' CSV').set_input_files({'name': kind + '.csv', 'mimeType': 'text/csv', 'buffer': b'example'})
    expect(import_button).to_be_enabled()
    import_button.click()
    expect(page.get_by_text('CSV import complete.', exact=False)).to_be_visible()
    page.get_by_role('button', name='Settings', exact=True).click()
    expect(page.get_by_label('PN ranking mode')).to_have_value('weighted')
    expect(page.get_by_label('Description reranker')).to_have_value('none')
    page.get_by_label('PN ranking mode').select_option('rrf')
    page.get_by_label('Description reranker').select_option('cross_encoder')
    page.get_by_role('button', name='Save matching settings').click()
    expect(page.get_by_text('Matching settings saved.', exact=False)).to_be_visible()
    expect(page.get_by_label('Description reranker')).to_have_value('cross_encoder')
    page.get_by_role('button', name='Connection settings', exact=True).click()
    expect(page.get_by_role('button', name='Save Configuration')).to_be_visible()
    page.get_by_label('Data source').select_option('amoscentral')
    page.get_by_label('Tenant AMOScentral sender address').fill('com.swiss-as.amos.customer.prod')
    expect(page.locator('input[x-model="amosForm.host"]')).not_to_be_visible()
    expect(page.get_by_role('button', name='Sync Now')).not_to_be_visible()
    page.get_by_role('button', name='Save Configuration').click()
    expect(page.get_by_text('AMOS settings saved.', exact=True)).to_be_visible()
    assert state['amoscentral_address'] == 'com.swiss-as.amos.customer.prod'
    page.get_by_label('Data source').select_option('database')
    page.get_by_role('button', name='Save Configuration').click()
    page.get_by_role('button', name='Spec2500 processing', exact=True).click()
    page.locator('#specFileInput').set_input_files({'name': 'test.xml', 'mimeType': 'application/xml', 'buffer': b'<Crate/>'})
    page.get_by_role('button', name='Choose from all AMOS parts…').click()
    expect(page.get_by_test_id('reference-part-number')).to_have_text('ENGINE-1')
    expect(page.get_by_test_id('reference-description')).to_have_text('Engine')
    page.get_by_role('button', name='Close cache').click()
    page.get_by_role('button', name='Confirm & Proceed').click()
    expect(page.get_by_text('Lubrication pump', exact=True)).to_be_visible()
    expect(page.get_by_text('Needs review', exact=True)).to_be_visible()
    part_select = page.locator('select[x-model="pnMapping[origPn]"]')
    expect(part_select.locator('option')).to_have_count(11)
    page.get_by_role('button', name='Show more suggestions (50)', exact=True).click()
    expect(part_select.locator('option')).to_have_count(51)
    part_select.select_option('CHILD-50')
    page.get_by_role('button', name='Show fewer suggestions', exact=True).click()
    expect(part_select).to_have_value('CHILD-50')
    expect(part_select.locator('option')).to_have_count(12)
    part_select.select_option('')
    expect(page.locator('tbody').first.get_by_text('ENGINE-1', exact=True)).to_have_count(0)
    page.get_by_role('button', name='Choose from all AMOS parts…').last.click()
    expect(page.get_by_test_id('reference-part-number')).to_have_text('SOURCE-1')
    expect(page.get_by_test_id('reference-description')).to_have_text('Lubrication pump')
    expect(page.get_by_text('Alternate oil pump', exact=True)).to_be_visible()
    page.get_by_label('Part search method').select_option('hybrid')
    page.get_by_label('Description meaning').fill('lubrication fluid pump')
    expect(page.get_by_test_id('reference-part-number')).to_have_text('SOURCE-1')
    expect(page.get_by_test_id('reference-description')).to_have_text('Lubrication pump')
    expect(page.get_by_text('Meaning similarity:', exact=False)).to_be_visible()
    expect(page.get_by_text('Outside root', exact=True)).to_be_visible()
    page.get_by_role('button', name='Use this part').filter(visible=True).click()
    expect(page.get_by_text('Manual selection', exact=True)).to_be_visible()
    expect(page.get_by_text("Outside this root's cached configuration.")).to_be_visible()
    page.get_by_role('button', name='Back', exact=True).click()
    page.get_by_role('button', name='Confirm & Proceed').click()
    expect(page.get_by_text('Manual selection', exact=True)).to_be_visible()
    assert state['candidate_calls'] == 1
    page.get_by_role('button', name='Next: Positions & Documents').click()
    page.get_by_role('button', name='Next: Export').click()
    state['expired'] = True
    page.get_by_role('button', name='Download Transformed Spec2500 XML').click()
    expect(page.get_by_text('Your session expired.', exact=False)).to_be_visible()
    page.locator('input[x-model="authForm.password"]').fill('password')
    page.get_by_role('button', name='Sign In', exact=True).click()
    expect(page.get_by_role('button', name='Download Transformed Spec2500 XML')).to_be_visible()
    page.get_by_role('button', name='AMOS DB', exact=True).click()
    page.get_by_role('button', name='Sync Now').click()
    expect(page.get_by_text('Loading documents', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='Sync running…')).to_be_disabled()
    page.get_by_role('button', name='Cached data', exact=True).click()
    expect(page.get_by_label('Cache category')).to_be_visible()
    page.get_by_label('Cache category').select_option('documents')
    page.get_by_role('button', name='Spec2500 processing', exact=True).click()
    page.get_by_role('button', name='Start over', exact=True).click()
    expect(page.get_by_role('heading', name='Upload Spec2500 XML File')).to_be_visible()
    expect(page.get_by_role('button', name='Start over', exact=True)).not_to_be_visible()
    page.get_by_role('button', name='AMOS DB', exact=True).click()
    page.screenshot(path='/tmp/spec2500-amos-section.png', full_page=True)
    assert not errors, errors
    browser.close()
print('Browser smoke passed: AMOScentral admin address assignment, source switching, upload, matching, session recovery, sync progress, and cache.')
