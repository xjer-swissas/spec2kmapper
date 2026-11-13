function app() {
  return {
    token: localStorage.getItem('token') || null,
    tenantSchema: localStorage.getItem('schema') || '',
    authMode: 'login', authMessage: '', error: '', notice: '', busy: false,
    authForm: { company_name: '', username: '', password: '' },
    matchingSettings: { ranking_mode: 'weighted', reranker: 'none', collect_confirmed_mappings: false, tuning_enabled: false, pn_weight: 0.25, is_admin: false, confirmed_count: 0 }, settingsLoaded: false, settingsBusy: false, trialWeight: 0.25, evaluation: null, reviewedMappings: {},
    section: 'wizard', amosTab: 'cache', fileName: '',
    step: 1, confirmedRootPn: '',
    csvQueries: {}, csvFiles: {}, csvBusy: false,
    amosForm: { source: 'database', amoscentral_address: '', central: { sender_address: '', state: 'DISABLED' }, host: '', port: 5432, dbname: '', user: '', password: '', refresh_interval_hours: 24 },
    sync: { status: 'IDLE', counts: {} }, polling: false,
    uploadResult: {}, selectedRootPn: '',
    candidates: { pn_matches: {}, fp_matches: {}, doc_matches: {}, pn_children_map: {} },
    pnMapping: {}, fpMapping: {}, docMapping: {},
    showCache: false, cacheKind: 'parts', cacheQuery: '', cacheOffset: 0,
    cache: { items: [], columns: [], total: 0 }, cacheLoading: false, cacheRequest: 0,
    pickerTarget: null, sessionTimer: null, searchMode: 'text', searchDescription: '', hybridItems: [], hybridInfo: null,

    async init() {
      if (this.token) {
        await this.api('/api/auth/me');
        if (this.token) await this.refreshStatus();
      }
      this.sessionTimer = setInterval(() => {
        if (!this.token) return;
        try {
          const payload = JSON.parse(atob(this.token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
          if (payload.exp * 1000 <= Date.now()) this.expireSession();
        } catch (_) { this.expireSession(); }
        if (this.token) this.refreshStatus();
      }, 10000);
    },
    destroy() { clearInterval(this.sessionTimer); },
    expireSession() {
      this.token = null;
      localStorage.removeItem('token');
      this.showCache = false;
      this.authMode = 'login'; this.error = '';
      this.authMessage = 'Your session expired. Sign in again to continue your current work.';
    },
    async api(url, options = {}, authenticated = true) {
      const requestToken = this.token;
      if (authenticated && !requestToken) return null;
      try {
        const headers = { ...options.headers };
        if (authenticated) headers.Authorization = `Bearer ${requestToken}`;
        const response = await fetch(url, { ...options, headers });
        if (authenticated && requestToken !== this.token) return null;
        if (response.status === 401 && authenticated) { this.expireSession(); return null; }
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          this.error = typeof data.detail === 'string' ? data.detail : `Request failed (${response.status}). Please try again.`;
          return null;
        }
        return response;
      } catch (_) {
        this.error = 'Cannot reach the server. Check your connection and try again.';
        return null;
      }
    },
    async handleAuth() {
      this.error = '';
      const login = this.authMode === 'login';
      const response = await this.api(login ? '/api/auth/login' : '/api/auth/register', {
        method: 'POST', headers: { 'Content-Type': login ? 'application/x-www-form-urlencoded' : 'application/json' },
        body: login ? new URLSearchParams({ username: this.authForm.username, password: this.authForm.password }) : JSON.stringify(this.authForm)
      }, false);
      if (!response) return;
      const data = await response.json();
      this.authForm.password = '';
      if (login) {
        if (this.tenantSchema !== data.schema_name) this.startOver();
        this.token = data.access_token; this.tenantSchema = data.schema_name; this.authMessage = '';
        localStorage.setItem('token', this.token); localStorage.setItem('schema', this.tenantSchema);
        await this.refreshStatus();
      } else { this.authMode = 'login'; this.authMessage = 'Registration successful. Please sign in.'; }
    },
    logout() {
      this.token = null; this.tenantSchema = ''; this.authMessage = '';
      localStorage.removeItem('token'); localStorage.removeItem('schema');
      this.showCache = false; this.section = 'wizard'; this.startOver();
    },
    startOver() {
      this.csvFiles = {};
      document.querySelectorAll('[aria-label="AMOS CSV import"] input[type="file"]').forEach(input => { input.value = ''; });
      this.reviewedMappings = {}; this.fileName = ''; this.step = 1; this.uploadResult = {}; this.selectedRootPn = ''; this.confirmedRootPn = '';
      this.candidates = { pn_matches: {}, fp_matches: {}, doc_matches: {}, pn_children_map: {} };
      this.pnMapping = {}; this.fpMapping = {}; this.docMapping = {}; this.error = ''; this.notice = '';
      const input = document.getElementById('specFileInput');
      if (input) input.value = '';
    },
    async openAmos() {
      this.section = 'amos'; this.error = ''; this.notice = ''; this.pickerTarget = null;
      await Promise.all([this.refreshStatus(), this.loadCache(), this.loadAmosConfig(), this.loadMatchingSettings(), this.loadCsvQueries()]);
    },
    async loadMatchingSettings() {
      this.settingsLoaded = false;
      const response = await this.api('/api/amos/matching-settings');
      if (response) { this.matchingSettings = await response.json(); this.trialWeight = this.matchingSettings.pn_weight; this.settingsLoaded = true; }
    },
    async saveMatchingSettings() {
      this.settingsBusy = true; this.error = ''; this.notice = '';
      try {
        const response = await this.api('/api/amos/matching-settings', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(this.matchingSettings)});
        if (response) { this.matchingSettings = await response.json(); this.notice = 'Matching settings saved. Changes apply to new analyses and searches.'; }
      } finally { this.settingsBusy = false; }
    },
    async evaluateMatching() {
      this.settingsBusy = true; this.evaluation = null; this.error = '';
      try {
        const response = await this.api('/api/amos/evaluate-matching', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pn_weight: this.trialWeight})});
        if (response) this.evaluation = await response.json();
      } finally { this.settingsBusy = false; }
    },
    async confirmReviewed(pn, noMatch = false) {
      const target = noMatch ? null : this.pnMapping[pn];
      if (!noMatch && (!target || target === '__EXCLUDE__')) return;
      const response = await this.api('/api/amos/confirmed-mappings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({session_id: this.uploadResult.session_id, source_pn: pn, target_pn: target})});
      if (response) { this.reviewedMappings[pn] = target || '__NO_MATCH__'; this.notice = (await response.json()).message; }
    },
    async loadCsvQueries() {
      const response = await this.api('/api/amos/csv/queries');
      if (response) this.csvQueries = await response.json();
    },
    async copySql(sql) {
      try { await navigator.clipboard.writeText(sql); this.notice = 'SQL copied.'; }
      catch (_) { this.notice = 'Select the SQL text and copy it with Ctrl+C or Cmd+C.'; }
    },
    async importCsv() {
      this.error = ''; this.notice = ''; this.csvBusy = true;
      try {
        const body = new FormData();
        for (const kind of ['parts', 'configurations', 'documents']) {
          if (!this.csvFiles[kind]) { this.error = 'Select all three CSV files.'; return; }
          body.append(kind, this.csvFiles[kind]);
        }
        const response = await this.api('/api/amos/csv/import', { method: 'POST', body });
        if (response) {
          this.notice = (await response.json()).message;
          await Promise.all([this.refreshStatus(), this.loadCache(true), this.loadAmosConfig()]);
        }
      } finally { this.csvBusy = false; }
    },
    async loadAmosConfig() {
      const response = await this.api('/api/amos/config');
      if (response) this.amosForm = await response.json();
    },
    formatCount(value) { return value == null ? '—' : Number(value).toLocaleString(); },
    formatDate(value) {
      if (!value) return 'No successful sync recorded';
      return new Date(/[Z+-]\d{0,2}:?\d{0,2}$/.test(value) ? value : value + 'Z').toLocaleString();
    },
    async saveAmosConfig() {
      this.error = '';
      const response = await this.api('/api/amos/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.amosForm)
      });
      if (response) { this.notice = 'AMOS settings saved.'; this.amosForm.password = ''; }
    },
    async refreshStatus() {
      if (this.polling || !this.token) return;
      this.polling = true;
      try {
        const response = await this.api('/api/amos/status');
        if (response) this.sync = await response.json();
      } finally { this.polling = false; }
    },
    async triggerSync() {
      this.error = '';
      const response = await this.api('/api/amos/sync', { method: 'POST' });
      if (response) { this.sync.status = 'RUNNING'; await this.refreshStatus(); }
    },
    async openCache(target = null) {
      this.pickerTarget = target; this.cacheKind = 'parts'; this.cacheQuery = ''; this.cacheOffset = 0;
      this.searchMode = 'text'; this.searchDescription = target === '__ROOT__' ? (this.uploadResult.root_description || '') : (this.candidates.pn_descriptions?.[target] || '');
      this.hybridItems = []; this.hybridInfo = null;
      this.showCache = true; await this.loadCache();
    },
    async rebuildIndex() {
      const response = await this.api('/api/amos/index', { method: 'POST' });
      if (response) await this.refreshStatus();
    },
    async loadCache(reset = false) {
      if (reset) this.cacheOffset = 0;
      const request = ++this.cacheRequest;
      this.cacheLoading = true;
      try {
        if (this.pickerTarget && this.searchMode === 'hybrid') {
          const params = new URLSearchParams({ q: this.cacheQuery, description: this.searchDescription, root: this.confirmedRootPn });
          const response = await this.api(`/api/amos/part-search?${params}`);
          if (response) {
            const data = await response.json();
            if (request === this.cacheRequest) { this.hybridItems = data.items; this.hybridInfo = data; }
          }
          return;
        }
        const params = new URLSearchParams({ kind: this.cacheKind, q: this.cacheQuery, offset: this.cacheOffset, limit: 50 });
        const response = await this.api(`/api/amos/cache?${params}`);
        if (response) {
          const data = await response.json();
          if (request === this.cacheRequest) this.cache = data;
        }
      } finally { if (request === this.cacheRequest) this.cacheLoading = false; }
    },
    pickPart(part) {
      const match = { candidate: part.part_number, description: part.description || '', match_type: 'MANUAL', score: null, requires_review: true };
      if (this.pickerTarget === '__ROOT__') {
        if (!this.uploadResult.root_matches.some(m => m.candidate === part.part_number)) this.uploadResult.root_matches.push(match);
        this.selectedRootPn = part.part_number;
      } else if (this.pickerTarget) {
        const matches = this.candidates.pn_matches[this.pickerTarget];
        if (!matches.some(m => m.candidate === part.part_number)) matches.push(match);
        this.pnMapping[this.pickerTarget] = part.part_number;
      }
      this.showCache = false;
    },
    matchLabel(m) {
      return `${m.candidate}${m.description ? ' — ' + m.description : ''} (${m.match_type === 'MANUAL' ? 'manual selection' : m.match_type + (m.cross_encoder_score != null ? ', cross-encoder ' + m.cross_encoder_score.toFixed(3) : m.rrf_score != null ? ', RRF ' + m.rrf_score.toFixed(4) : ', score ' + m.score)})`;
    },
    partStatus(pn) {
      const selected = this.pnMapping[pn];
      if (selected === '__EXCLUDE__') return 'Excluded';
      if (!selected) return 'Needs review';
      const match = this.candidates.pn_matches[pn]?.find(m => m.candidate === selected);
      if (match?.match_type === 'EXACT') return 'Exact PN';
      return match?.match_type === 'MANUAL' ? 'Manual selection' : 'Selected suggestion';
    },
    async uploadFile(e) {
      const file = e.target.files[0]; if (!file || this.busy) return;
      this.busy = true; this.error = '';
      try {
        const body = new FormData(); body.append('file', file);
        const response = await this.api('/api/spec2500/upload', { method: 'POST', body });
        if (!response) return;
        const data = await response.json(); this.startOver(); this.uploadResult = data; this.fileName = file.name;
        this.selectedRootPn = data.root_matches[0]?.candidate || ''; this.step = 2;
      } finally { this.busy = false; }
    },
    async confirmRootPn() {
      if (this.confirmedRootPn === this.selectedRootPn) { this.step = 3; return; }
      this.busy = true; this.error = '';
      try {
        const response = await this.api(`/api/spec2500/candidates/${this.uploadResult.session_id}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ root_pn: this.selectedRootPn })
        });
        if (!response) return;
        const data = await response.json(); this.candidates = data; this.reviewedMappings = {};
        this.pnMapping = {}; this.fpMapping = {}; this.docMapping = {};
        // Description and normalized PN suggestions require an explicit choice.
        for (const [orig, matches] of Object.entries(data.pn_matches)) this.pnMapping[orig] = matches[0]?.match_type === 'EXACT' ? matches[0].candidate : '';
        for (const [orig, matches] of Object.entries(data.fp_matches)) this.fpMapping[orig] = matches[0]?.score >= 85 ? matches[0].candidate : '';
        for (const [orig, matches] of Object.entries(data.doc_matches)) this.docMapping[orig] = matches[0]?.score >= 85 ? matches[0].candidate : '';
        this.confirmedRootPn = this.selectedRootPn; this.step = 3;
      } finally { this.busy = false; }
    },
    toggleExclude(pn) {
      this.pnMapping[pn] = this.pnMapping[pn] === '__EXCLUDE__' ? '' : '__EXCLUDE__';
    },
    async downloadExport() {
      this.error = '';
      const response = await this.api('/api/spec2500/export', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: this.uploadResult.session_id, pn_mapping: this.pnMapping, fp_mapping: this.fpMapping, doc_mapping: this.docMapping })
      });
      if (!response) return;
      const blob = await response.blob(); const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url;
      a.download = `Transformed_Spec2500_${this.confirmedRootPn.replace(/\//g, '_')}.xml`;
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  };
}
