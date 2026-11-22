'use strict';
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync} = require('node:fs');
const {createHash} = require('node:crypto');
const {version} = require('../package.json');
const {syncAndPatchWaJs} = require('./sync-wa-js.cjs');

async function main() {
  syncAndPatchWaJs();
  const desktop = path.resolve(__dirname,'..');
  const root = path.resolve(desktop,'..');
  const python = path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
  const outputRoot = path.resolve(process.env.JARVIS_BUILD_OUTPUT || path.join(root, 'artifacts'));
  const engineDist = path.join(outputRoot,'desktop-engine');
  const engine = path.join(engineDist, 'jarvis-desktop-service');
  const native = path.join(outputRoot, 'native', 'jarvis-native');
  const tempDir = path.join(outputRoot, 'packager-temp');
  mkdirSync(tempDir, {recursive: true});
  const reuseBuilt = process.argv.includes('--reuse-built');
  if (reuseBuilt && (!existsSync(engine) || !existsSync(native))) throw new Error('The engine and native helper must already be built.');
  if (!reuseBuilt) {
    if (!existsSync(python)) throw new Error('Create the project virtual environment before packaging.');
    const built = spawnSync(python,['-m','PyInstaller','--noconfirm','--onedir','--name','jarvis-desktop-service',
      '--distpath',engineDist,'--workpath',path.join(root,'.runtime','desktop-build'),'--specpath',path.join(root,'.runtime'),
      '--paths',path.join(root,'backend'),'--collect-submodules','keyring.backends','--collect-submodules','edge_tts',
      '--hidden-import','jarvis.voice','--hidden-import','jarvis.local_repo','--hidden-import','jarvis.trading_engine','--hidden-import','edge_tts','--hidden-import','aiohttp','--hidden-import','win32com','--hidden-import','win32com.client','--hidden-import','pythoncom','--hidden-import','pywintypes',
      path.join(root,'backend','jarvis','desktop_service_entry.py')],{cwd:root,stdio:'inherit',windowsHide:true});
    if (built.status !== 0) throw new Error('Desktop engine build failed.');
    const helper = spawnSync(python, [path.join(root,'scripts','build-native.py'), '--output', path.join(outputRoot,'native')], {
      cwd: root, stdio:'inherit', windowsHide:true,
    });
    if (helper.status !== 0) throw new Error('Native messaging helper build failed.');
  }
  const {packager} = await import('@electron/packager');
  const outputs = await packager({dir:desktop,out:path.join(outputRoot,'desktop'),name:'Jarvis Trade',
    executableName:'Jarvis Trade',appBundleId:'com.jarvis.trade.desktop',appVersion:version,...(process.platform==='win32'?{icon:path.join(desktop,'icon.ico')} : {}),
    ...(process.env.JARVIS_ELECTRON_ZIP_DIR ? {electronZipDir: process.env.JARVIS_ELECTRON_ZIP_DIR} : {}),
    overwrite:true,asar:true,prune:true,platform:process.platform,arch:process.arch,tmpdir:tempDir,
    extraResource:[engine,native,path.join(root,'extension')],
    ignore:[/^\/scripts(?:\/|$)/,/\.test\.cjs$/,/package-lock\.json$/],
    win32metadata:{CompanyName:'Jarvis Trade',FileDescription:'Jarvis Trade desktop',ProductName:'Jarvis Trade'}});
  console.log('Local desktop bundle:',outputs.join('\n'));
  for (const output of outputs) {
    if (existsSync(path.join(root, 'OWNER_ACCEPTANCE.md'))) {
      copyFileSync(path.join(root, 'OWNER_ACCEPTANCE.md'), path.join(output, 'OWNER_ACCEPTANCE.md'));
    } else if (existsSync(path.join(root, 'README.md'))) {
      copyFileSync(path.join(root, 'README.md'), path.join(output, 'README.md'));
    }
    const files = readdirSync(output, {recursive:true, withFileTypes:true})
      .filter(entry => entry.isFile())
      .map(entry => path.relative(output, path.join(entry.parentPath, entry.name)).split(path.sep).join('/'))
      .filter(file => file !== 'SHA256SUMS.txt').sort();
    const manifest = files.map(file => createHash('sha256').update(readFileSync(path.join(output, file))).digest('hex') + '  ' + file);
    writeFileSync(path.join(output,'SHA256SUMS.txt'), manifest.join('\n') + '\n');
  }
  console.log('This build is unsigned. Sign and validate on each target OS before client distribution.');
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});


