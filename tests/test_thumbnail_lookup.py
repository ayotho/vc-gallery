import sqlite3
from types import SimpleNamespace
import vc_gallery_serve as server


def test_thumbnail_beyond_first_500_is_indexed(tmp_path, monkeypatch):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE assets(id INTEGER PRIMARY KEY, file_path TEXT, thumb_path TEXT, thumb_generated_at TEXT, status TEXT)')
    for i in range(601):
        db.execute('INSERT INTO assets(id,file_path) VALUES (?,?)', (i, str(tmp_path / f'{i}.png')))
    source = tmp_path / '600.png'
    source.write_bytes(b'source fixture')
    monkeypatch.setattr(server, 'STATE', SimpleNamespace(thumb_dir=tmp_path, conn=lambda: db))
    name = server.lib.thumb_key(str(source)) + '.jpg'
    def generate(path, cache):
        (cache / name).write_bytes(b'thumb fixture')
        return name
    monkeypatch.setattr(server.thumb_mod, 'ensure_thumb', generate)
    handler = object.__new__(server.Handler)
    served = []
    handler._send_file = lambda path: served.append(path)
    handler._serve_thumb(name)
    assert served == [tmp_path / name]
    assert db.execute('SELECT count(*) FROM assets WHERE thumb_path IS NOT NULL').fetchone()[0] == 601
    assert not db.in_transaction
