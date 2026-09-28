"""Reproducible Windows debug APK builder, no Android Studio or system Java changes.

Only official Google SDK and Azul OpenJDK archives are downloaded, with pinned
checksums. Private keys, accounts and GPS data are never read or packaged.
"""
from pathlib import Path
import argparse, concurrent.futures, hashlib, json, os, shutil, subprocess, tempfile
import urllib.request, urllib.parse, zipfile, xml.etree.ElementTree as ET

PACKAGES = [
    ('platform', 'https://dl.google.com/android/repository/platform-35_r02.zip',
     'sha1', '0bb560a90a7a2cbd0dd8348224d518b638fe7949'),
    ('build-tools', 'https://dl.google.com/android/repository/build-tools_r35_windows.zip',
     'sha1', 'af059bb67cf7786f45ee0db85e2d24985df1b4b6'),
    ('jdk', 'https://cdn.azul.com/zulu/bin/zulu17.68.203-ca-jdk17.0.20.1-win_x64.zip',
     'sha256', '552a335d5342c800fd03b45a23bb1136f5aa5562193542ece506d8c44b0c60d9'),
]
ANDROID = 'http://schemas.android.com/apk/res/android'
ET.register_namespace('android', ANDROID)

def install(root, package):
    name, url, algorithm, expected = package
    destination = root/name
    marker = destination/'.verified'
    if marker.exists() and marker.read_text() == expected:
        return
    archive = root/(name+'.zip')
    if not archive.exists():
        print('Загрузка инструмента: '+name, flush=True)
        partial = archive.with_suffix('.download')
        with urllib.request.urlopen(url, timeout=90) as response, partial.open('wb') as out:
            shutil.copyfileobj(response, out, 1024*1024)
        partial.replace(archive)
    digest = hashlib.new(algorithm)
    with archive.open('rb') as src:
        for chunk in iter(lambda: src.read(1024*1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise RuntimeError('Контрольная сумма не совпала: '+str(archive))
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        for item in z.infolist():
            if not (destination/item.filename).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Недопустимый путь в архиве')
        z.extractall(destination)
    marker.write_text(expected)

def run(args, **kwargs):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding='utf-8',
                            errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    if result.stdout.strip():
        print(result.stdout.strip())
    return result

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--server-url', required=True)
    p.add_argument('--local-ca', type=Path)
    p.add_argument('--tools-dir', type=Path, default=Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir()))/'FieldRouterAndroid'/'toolchain')
    p.add_argument('--output', type=Path)
    p.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    args = p.parse_args()
    url = urllib.parse.urlsplit(args.server_url)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        p.error('Укажите HTTPS-адрес сервера без пути, логина и пароля.')
    origin = urllib.parse.urlunsplit((url.scheme, url.netloc, '', '', ''))
    project = args.project.resolve()
    output = (args.output or project/'output'/'android').resolve(); output.mkdir(parents=True, exist_ok=True)
    tools = args.tools_dir.resolve(); tools.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda spec: install(tools, spec), PACKAGES))
    javac = next((tools/'jdk').rglob('javac.exe'))
    java, keytool = javac.with_name('java.exe'), javac.with_name('keytool.exe')
    aapt = next((tools/'build-tools').rglob('aapt2.exe'))
    platform = next((tools/'platform').rglob('android.jar'))
    d8 = aapt.parent/'lib'/'d8.jar'
    apksigner = aapt.parent/'lib'/'apksigner.jar'
    # Every build uses a fresh directory. No recursive removal of arbitrary paths.
    work = Path(tempfile.mkdtemp(prefix='field-engineer-apk-'))
    resources = work/'res'; shutil.copytree(project/'android/app/src/main/res', resources)
    config = ET.Element('resources')
    ET.SubElement(config, 'string', {'name':'default_server'}).text = origin
    ET.ElementTree(config).write(resources/'values'/'build.xml', encoding='utf-8')
    if args.local_ca:
        certificate = args.local_ca.read_bytes()
        if b'PRIVATE KEY' in certificate or b'BEGIN CERTIFICATE' not in certificate:
            raise ValueError('Нужен публичный CA-сертификат, не закрытый ключ.')
        (resources/'raw').mkdir(exist_ok=True)
        (resources/'raw'/'development_ca.crt').write_bytes(certificate)
        ET.ElementTree(ET.fromstring('''<network-security-config>
            <base-config cleartextTrafficPermitted="false"><trust-anchors><certificates src="system"/></trust-anchors></base-config>
            <debug-overrides><trust-anchors><certificates src="@raw/development_ca"/></trust-anchors></debug-overrides>
            </network-security-config>''')).write(resources/'xml'/'network_security_config.xml', encoding='utf-8')
    manifest = ET.parse(project/'android/app/src/main/AndroidManifest.xml')
    manifest.getroot().set('package', 'ru.fieldrouter.engineer')
    manifest.getroot().set('{'+ANDROID+'}versionCode', '6')
    manifest.getroot().set('{'+ANDROID+'}versionName', '0.1.5')
    app = manifest.find('application'); app.set('{'+ANDROID+'}debuggable', 'true')
    app.set('{'+ANDROID+'}label', 'Выездной инженер · тест')
    ET.SubElement(manifest.getroot(), 'uses-sdk', {'{'+ANDROID+'}minSdkVersion':'26', '{'+ANDROID+'}targetSdkVersion':'35'})
    manifest.write(work/'AndroidManifest.xml', encoding='utf-8')
    generated, classes, dex = work/'generated', work/'classes', work/'dex'
    for folder in (generated, classes, dex):
        folder.mkdir()
    run([aapt, 'compile', '--dir', resources, '-o', work/'res.zip'])
    run([aapt, 'link', '-o', work/'base.apk', '--manifest', work/'AndroidManifest.xml',
         '-I', platform, '--java', generated, '--auto-add-overlay', work/'res.zip'])
    # Keep javac paths short and ASCII: Windows repositories can exceed MAX_PATH
    # or contain Cyrillic names that an argument file otherwise misdecodes.
    java_sources=work/'java'
    shutil.copytree(project/'android/app/src/main/java', java_sources)
    sources = sorted(java_sources.rglob('*.java')) + sorted(generated.rglob('*.java'))
    source_list = work/'sources.txt'
    source_list.write_text('\n'.join('"'+str(s).replace('\\','/')+'"' for s in sources), encoding='utf-8')
    run([javac, '-J-Dfile.encoding=UTF-8', '-encoding', 'UTF-8', '-source', '11', '-target', '11', '-classpath', platform, '-d', classes, '@'+str(source_list)])
    with zipfile.ZipFile(work/'classes.jar','w') as z:
        for f in classes.rglob('*.class'):
            z.write(f, f.relative_to(classes).as_posix())
    run([java, '-cp', d8, 'com.android.tools.r8.D8', '--lib', platform, '--min-api','26', '--output',dex,work/'classes.jar'])
    with zipfile.ZipFile(work/'base.apk','a') as z:
        for f in dex.glob('*.dex'):
            z.write(f, f.name)
    run([aapt.with_name('zipalign.exe'), '-f','4',work/'base.apk',work/'aligned.apk'])
    # Standard local debug signing only. Never use this identity for a public release.
    keystore = tools/'debug.keystore'
    if not keystore.exists():
        run([keytool,'-genkeypair','-keystore',keystore,'-storepass','android','-keypass','android',
             '-alias','androiddebugkey','-dname','CN=Android Debug,O=Field Router,C=RU','-keyalg','RSA','-keysize','2048','-validity','3650'])
    apk = output/'field-engineer-debug.apk'
    signed_apk=work/'signed.apk'
    run([java,'-jar',apksigner,'sign','--ks',keystore,'--ks-key-alias','androiddebugkey','--ks-pass','pass:android',
         '--key-pass','pass:android','--out',signed_apk,work/'aligned.apk'])
    run([java,'-jar',apksigner,'verify','--verbose',signed_apk])
    digest = hashlib.sha256(signed_apk.read_bytes()).hexdigest()
    # Pure JVM checks complement APK compilation; they do not emulate Android hardware.
    test = project/'android/tests/TrackingPolicyTest.java'
    if test.exists():
        test_source=work/'TrackingPolicyTest.java'
        shutil.copy2(test,test_source)
        run([javac,'-J-Dfile.encoding=UTF-8','-encoding','UTF-8','-d',classes,test_source,
             java_sources/'ru/fieldrouter/engineer/TrackingPolicy.java',
             java_sources/'ru/fieldrouter/engineer/FixSelector.java',
             java_sources/'ru/fieldrouter/engineer/WakefulUpload.java',
             java_sources/'ru/fieldrouter/engineer/ShiftPower.java'])
        run([java,'-ea','-cp',classes,'TrackingPolicyTest'])
    # Only Python touches the destination, which may contain Cyrillic characters.
    # Publish a verified complete artifact after checks, replacing it atomically.
    pending=output/'field-engineer-debug.apk.pending'
    shutil.copy2(signed_apk,pending)
    pending.replace(apk)
    (output/'build-info.json').write_text(json.dumps({'version':'0.1.5','server':origin,'local_ca':bool(args.local_ca),
        'sha256':digest,'physical_phone_tested':False}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('APK: '+str(apk))
    print('SHA256: '+digest)

if __name__ == '__main__':
    main()
