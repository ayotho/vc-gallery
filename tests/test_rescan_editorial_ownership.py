import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vc_gallery_lib as lib
import vc_gallery_scan as scan


def test_changed_media_preserves_editorial_fields(tmp_path):
    conn = lib.connect(tmp_path / 'test.db')
    path = str(tmp_path / 'example.png')
    conn.execute("INSERT INTO assets (file_path, filename, media_type, size_bytes, file_modified_at, status, scene, shot_id, width, height) VALUES (?, 'example.png', 'image', 10, 100, 'accepted', 'Segment 1', 'SH010', 10, 10)", (path,))
    row: dict = {k: None for k in scan.ASSET_COLUMNS}
    row.update(file_path=path, filename='example.png', media_type='image', size_bytes=20, file_modified_at=200, source_type='raw_manual', has_sidecar=False, status='review', scene='Wrong folder', shot_id='SH999', has_audio=0)
    with patch.object(scan, 'probe_media_dimensions', return_value={'width': 20, 'height': 20, 'duration_sec': None}):
        _, result = scan._upsert_asset(conn, row)
    actual = conn.execute('SELECT * FROM assets').fetchone()
    assert result == 'updated'
    assert (actual['status'], actual['scene'], actual['shot_id']) == ('accepted', 'Segment 1', 'SH010')
    assert actual['size_bytes'] == 20
    conn.close()


def _changed_row(path, **over):
    row: dict = {k: None for k in scan.ASSET_COLUMNS}
    row.update(file_path=path, filename='example.png', media_type='image', size_bytes=20, file_modified_at=200,
               source_type='raw_manual', has_sidecar=False, status='review', has_audio=0)
    row.update(over)
    return row


def test_changed_media_keeps_manual_model_workflow_without_sidecar(tmp_path):
    conn = lib.connect(tmp_path / 'test.db')
    path = str(tmp_path / 'example.png')
    conn.execute("INSERT INTO assets (file_path, filename, media_type, size_bytes, file_modified_at, status, model, workflow, width, height) VALUES (?, 'example.png', 'image', 10, 100, 'review', 'Midjourney v8.2', '1 · MJ raw', 10, 10)", (path,))
    with patch.object(scan, 'probe_media_dimensions', return_value={'width': 20, 'height': 20, 'duration_sec': None}):
        _, result = scan._upsert_asset(conn, _changed_row(path))
    actual = conn.execute('SELECT * FROM assets').fetchone()
    assert result == 'updated'
    assert (actual['model'], actual['workflow']) == ('Midjourney v8.2', '1 · MJ raw')
    assert actual['size_bytes'] == 20
    conn.close()


def test_changed_media_sidecar_model_still_wins(tmp_path):
    conn = lib.connect(tmp_path / 'test.db')
    path = str(tmp_path / 'example.png')
    conn.execute("INSERT INTO assets (file_path, filename, media_type, size_bytes, file_modified_at, status, model, workflow, width, height) VALUES (?, 'example.png', 'image', 10, 100, 'review', 'old', 'old-wf', 10, 10)", (path,))
    with patch.object(scan, 'probe_media_dimensions', return_value={'width': 20, 'height': 20, 'duration_sec': None}):
        scan._upsert_asset(conn, _changed_row(path, has_sidecar=True, model='gpt_image_2_5', workflow='sunburst'))
    actual = conn.execute('SELECT * FROM assets').fetchone()
    assert (actual['model'], actual['workflow']) == ('gpt_image_2_5', 'sunburst')
    conn.close()
