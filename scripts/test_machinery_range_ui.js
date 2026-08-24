#!/usr/bin/env node
/* Guard the browser-side range rule against Loctell's zero placeholders. */
const fs = require('fs');
const path = require('path');

const source = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const start = source.indexOf('function _machineRangeRows(from,to){');
const end = source.indexOf('function _machineTableHtml(', start);
if (start < 0 || end < 0) throw new Error('machine range function not found');

const names = ['Jaw', 'Cone', 'VSI', 'Hitachi', 'VMI Loader'];
const day = (date, values) => ({
  date,
  readings: names.map(name => {
    const pair = values[name] || [0, 0];
    return {
      vehicle_type: name,
      start_reading: pair[0],
      end_reading: pair[1],
      difference: pair[1] - pair[0],
      has_reading: Boolean(pair[0] || pair[1]),
    };
  }),
});

// This mirrors the 17–23 Aug Loctell report: 17–19 are placeholders, then
// actual machinery readings begin on later days in the selected window.
const _allData = { machines: [], machineHistory: [
  day('2026-08-17', {}),
  day('2026-08-18', {}),
  day('2026-08-19', {}),
  day('2026-08-20', { Jaw: [3418.2, 3418.2], 'VMI Loader': [3000.4, 3001.1] }),
  day('2026-08-22', { Jaw: [3427.4, 3436.2], Cone: [3484.1, 3493.3], VSI: [3660.5, 3671.0], 'VMI Loader': [3005.4, 3013.2] }),
  day('2026-08-23', { Jaw: [3436.2, 3444.8], Cone: [3493.3, 3502.1], VSI: [3671.0, 3679.8], 'VMI Loader': [3013.2, 3020.8] }),
] };
function _num(value) { return Number(value || 0); }
eval(source.slice(start, end));

const rows = _machineRangeRows('2026-08-17', '2026-08-23');
const byName = Object.fromEntries(rows.map(row => [row.vehicle_type, row]));
if (rows.length !== 5) throw new Error(`expected exactly five canonical machines, got ${JSON.stringify(rows)}`);
for (const [name, expected] of Object.entries({
  Jaw: [3418.2, 3444.8, 26.6],
  Cone: [3484.1, 3502.1, 18.0],
  VSI: [3660.5, 3679.8, 19.3],
  'VMI Loader': [3000.4, 3020.8, 20.4],
})) {
  const row = byName[name];
  if (!row || row.start_reading !== expected[0] || row.end_reading !== expected[1] || row.difference !== expected[2]) {
    throw new Error(`${name} did not match the Loctell range result: ${JSON.stringify(row)}`);
  }
}

// Loctell occasionally varies a vehicle label. It must not create a second
// physical machine in either the dashboard or Operations table.
_allData.machineHistory[5].readings.push({
  vehicle_type: 'JAW CRUSHER', start_reading: 3427.4, end_reading: 3436.2,
  difference: 8.8, has_reading: true,
});
const aliasRows = _machineRangeRows('2026-08-17', '2026-08-23');
if (aliasRows.length !== 5 || aliasRows.filter(row => row.vehicle_type === 'Jaw').length !== 1) {
  throw new Error(`machine alias was rendered twice: ${JSON.stringify(aliasRows)}`);
}
console.log('machinery range UI guard passed');
