"""Folder visibility is path-based, reversible, and applied before pagination."""
import json
import sqlite3
from pathlib import Path

import pytest
import vc_gallery_serve as server


class Library:
    def __init__(self, root):
        self.folder = root
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE assets (file_path TEXT)')
        for name in ['takes/frame.png', 'takes/nested/frame.png', 'takes-other/keep.png', 'movie.mp4']:
            self.db.execute('INSERT INTO assets VALUES (?)', (str(root / name),))

    def conn(self):
        return self.db


def test_persist_reveal_and_boundary(tmp_path, monkeypatch):
    library = Library(tmp_path)
    monkeypatch.setattr(server, 'STATE', library)
    assert 'takes/nested' in server._folder_visibility()['folders']
    server._set_folder_visibility({'folder': 'takes', 'hidden': True})
    assert server._folder_visibility()['hidden'] == ['takes']
    assert json.loads((tmp_path / '.vc_meta/folder_visibility.json').read_text())['hidden'] == ['takes']
    where, values = server._visibility_filter({})
    rows = library.db.execute('SELECT file_path FROM assets a' + where, values).fetchall()
    assert {Path(row[0]).name for row in rows} == {'keep.png', 'movie.mp4'}
    assert server._visibility_filter({'show_hidden': ['1']}) == ('', [])
    server._set_folder_visibility({'folder': 'takes', 'hidden': False})
    assert server._folder_visibility()['hidden'] == []


@pytest.mark.parametrize('folder', ['..', '../outside', '/tmp', '.', 'absent'])
def test_reject_invalid_folder(tmp_path, monkeypatch, folder):
    monkeypatch.setattr(server, 'STATE', Library(tmp_path))
    with pytest.raises(ValueError):
        server._set_folder_visibility({'folder': folder, 'hidden': True})
