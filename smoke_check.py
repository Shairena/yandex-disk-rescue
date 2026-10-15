"""Narrow offline engine smoke. Uses only synthetic bytes in a temporary directory."""
import hashlib
import io
from pathlib import Path
import tempfile
from engine import Store, Engine


class DemoAPI:
    def identity(self):
        return 'synthetic-account'

    def metadata(self, path, offset=0):
        if path == 'disk:/':
            items = [{'path': 'disk:/empty', 'type': 'dir'},
                     {'path': 'disk:/sample.txt', 'type': 'file', 'size': 5,
                      'sha256': hashlib.sha256(b'hello').hexdigest()}]
            return {'path': path, 'type': 'dir', '_embedded': {'offset': offset, 'total': 2, 'items': items[offset:offset + 1]}}
        return {'path': path, 'type': 'dir', '_embedded': {'offset': 0, 'total': 0, 'items': []}}

    def download(self, path):
        return io.BytesIO(b'hello')


def main():
    with tempfile.TemporaryDirectory(prefix='synthetic-', dir=Path(__file__).parent / 'review_artifacts') as scratch:
        root = Path(scratch)
        store = Store(root / 'state')
        engine = Engine(store, DemoAPI(), interval=0)
        engine.run(root / 'backup', ['disk:/'])
        assert (root / 'backup' / 'empty').is_dir()
        assert (root / 'backup' / 'sample.txt').read_bytes() == b'hello'
        assert len(store.rows('downloaded')) == 1
        assert all(f['status'] == 'complete' for f in store.folders())
        exported = store.export(root / 'exports')
        assert 'sample.txt' in (exported / 'downloaded.csv').read_text(encoding='utf-8-sig')
        assert 'sample.txt' not in (exported / 'unresolved.csv').read_text(encoding='utf-8-sig')
        print('PASS: synthetic two-page recursion, empty directory, verified publication, CSV membership')


if __name__ == '__main__':
    (Path(__file__).parent / 'review_artifacts').mkdir(exist_ok=True)
    main()
