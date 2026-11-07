def test_encrypted_backup_is_read_back_authenticated_and_rejects_tampering(tmp_path):
    import pytest

    from folio.backup import open_backup, seal_backup

    raw = b"actual paired PostgreSQL dump bytes, synthetic fixture"
    report = seal_backup(
        raw, tmp_path / "backup.enc", tmp_path / "keys" / "backup.key", "folio_test"
    )
    assert report["encrypted"] is True
    assert (tmp_path / "backup.enc").read_bytes() != raw
    assert (
        open_backup(
            tmp_path / "backup.enc", tmp_path / "keys" / "backup.key", "folio_test"
        )
        == raw
    )
    encrypted = (tmp_path / "backup.enc").read_bytes()
    (tmp_path / "backup.enc").write_bytes(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))
    with pytest.raises(ValueError, match="BACKUP_AUTHENTICATION_FAILED"):
        open_backup(
            tmp_path / "backup.enc", tmp_path / "keys" / "backup.key", "folio_test"
        )
