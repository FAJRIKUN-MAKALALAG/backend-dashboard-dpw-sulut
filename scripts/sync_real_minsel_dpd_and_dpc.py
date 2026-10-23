import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from app import create_app
from app.models import Member
from app.extensions import db

def run_sync():
    app = create_app()
    with app.app_context():
        print("Starting Minahasa Selatan DPD & DPC Synchronization...")

        # 1. Hapus pengurus mock / generated di Minahasa Selatan
        deleted_mocks = Member.query.filter(
            Member.dpd.ilike('%MINAHASA SELATAN%'),
            (Member.nomor_ekta.like('KTA-GEN-%') | Member.nik.like('7102%'))
        ).delete(synchronize_session=False)
        db.session.commit()
        print(f"1. Deleted {deleted_mocks} mock/generated records from Minahasa Selatan.")

        # 2. Baca 34 Pengurus DPD dari DATA_PENGURUS_MINSEL_SIAP_IMPORT.xlsx
        excel_path = r'c:\FAJRIKUN\PROJECT KERJAAN\Nasdem\Dashboard-DPW-Sulut\importdatabase\DATA_PENGURUS_MINSEL_SIAP_IMPORT.xlsx'
        with zipfile.ZipFile(excel_path) as z:
            shared_strings = []
            if 'xl/sharedStrings.xml' in z.namelist():
                s_xml = z.read('xl/sharedStrings.xml')
                s_root = ET.fromstring(s_xml)
                for si in s_root.findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si'):
                    shared_strings.append(''.join(si.itertext()))

            sheet_xml = z.read('xl/worksheets/sheet1.xml')
            sheet_tree = ET.fromstring(sheet_xml)
            rows = sheet_tree.findall('.//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row')

            dpd_members_data = []
            motoling_members_data = []

            for r in rows[1:]:
                cells = r.findall('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c')
                row_dict = {}
                for c in cells:
                    ref = c.attrib.get('r', '')
                    col = ''.join([ch for ch in ref if ch.isalpha()])
                    t = c.attrib.get('t', '')
                    v = c.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
                    val = v.text if v is not None else ''
                    if t == 's' and val.isdigit():
                        val = shared_strings[int(val)]
                    row_dict[col] = val

                # Col B: Tingkat, Col C: Kecamatan, Col D: DPD, Col E: Jabatan, Col F: Nama, Col G: NIK, Col H: E-KTA
                tingkat = row_dict.get('B', '').strip()
                kec = row_dict.get('C', '').strip()
                jabatan = row_dict.get('E', '').strip()
                nama = row_dict.get('F', '').strip()
                nik = row_dict.get('G', '').strip()
                ekta = row_dict.get('H', '').strip()

                if tingkat == 'DPD':
                    dpd_members_data.append({
                        'jabatan': jabatan,
                        'nama': nama,
                        'nik': nik,
                        'ekta': ekta,
                    })
                elif kec.upper() == 'MOTOLING':
                    motoling_members_data.append({
                        'jabatan': jabatan,
                        'nama': nama,
                        'nik': nik,
                        'ekta': ekta,
                    })

        print(f"2. Found {len(dpd_members_data)} DPD rows from Excel.")
        print(f"3. Found {len(motoling_members_data)} Motoling DPC rows from Excel.")

        # 3. Upsert 34 Pengurus DPD
        for idx, item in enumerate(dpd_members_data):
            nik = item['nik']
            # Standarisasi NIK 16 digit jika kurang (contoh 15 digit pad with zero)
            if len(nik) == 15 and not nik.startswith('7'):
                nik = '7' + nik
            elif len(nik) == 15:
                nik = nik + '0'

            member = Member.query.get(nik)
            if not member:
                # Periksa apakah ada berdasarkan nama di DPD Minsel
                member = Member.query.filter(
                    Member.dpd.ilike('%MINAHASA SELATAN%'),
                    Member.nama_lengkap.ilike(f"%{item['nama'][:12]}%")
                ).first()

            if not member:
                member = Member(nik=nik)
                db.session.add(member)

            member.nomor_ekta = item['ekta']
            member.nama_lengkap = item['nama']
            member.dpd = 'Kabupaten Minahasa Selatan'
            member.dpc = 'Umum (DPD)'
            member.dprt = 'Belum Terpetakan'
            member.is_pengurus = True
            member.tingkat_kepengurusan = 'DPD'
            member.wilayah_penugasan = 'DPD Kabupaten Minahasa Selatan'
            member.nomor_sk = 'SK-DPD-MINSEL/001/2024'
            member.tanggal_sk = date(2024, 5, 20)
            member.jabatan = item['jabatan']
            member.urutan = idx + 1
            member.status = 'Aktif'
            member.diubah_oleh = 'Sync Excel DPW'

        db.session.commit()
        print(f"Successfully upserted {len(dpd_members_data)} DPD Minsel members with real jabatan & urutan.")

        # 4. Upsert 17 Pengurus DPC Motoling
        for idx, item in enumerate(motoling_members_data):
            nik = item['nik']
            # Perbaiki 2 typo NIK Motoling
            if nik == '710507420273002':
                nik = '7105074202730002'
            elif nik == '105072805870001':
                nik = '7105072805870001'
            elif len(nik) == 15 and not nik.startswith('7'):
                nik = '7' + nik
            elif len(nik) == 15:
                nik = nik + '0'

            member = Member.query.get(nik)
            if not member:
                member = Member.query.filter(
                    Member.dpd.ilike('%MINAHASA SELATAN%'),
                    Member.dpc.ilike('%MOTOLING%'),
                    Member.nama_lengkap.ilike(f"%{item['nama'][:12]}%")
                ).first()

            if not member:
                member = Member(nik=nik)
                db.session.add(member)

            member.nomor_ekta = item['ekta']
            member.nama_lengkap = item['nama']
            member.dpd = 'Kabupaten Minahasa Selatan'
            member.dpc = 'MOTOLING'
            member.dprt = 'Belum Terpetakan'
            member.is_pengurus = True
            member.tingkat_kepengurusan = 'DPC'
            member.wilayah_penugasan = 'DPC Kecamatan MOTOLING'
            member.nomor_sk = 'SK-DPC-MOTOLING/2024'
            member.tanggal_sk = date(2024, 5, 20)
            member.jabatan = item['jabatan']
            member.urutan = idx + 1
            member.status = 'Aktif'
            member.diubah_oleh = 'Sync Excel DPW'

        db.session.commit()
        print(f"Successfully upserted {len(motoling_members_data)} Motoling DPC members.")

        # 5. Verifikasi Akhir Jumlah Pengurus & DPC di Minahasa Selatan
        final_dpd_count = Member.query.filter(
            Member.dpd.ilike('%MINAHASA SELATAN%'),
            Member.tingkat_kepengurusan == 'DPD'
        ).count()

        distinct_dpcs = db.session.query(Member.dpc).filter(
            Member.dpd.ilike('%MINAHASA SELATAN%'),
            Member.tingkat_kepengurusan == 'DPC'
        ).distinct().all()

        total_minsel = Member.query.filter(Member.dpd.ilike('%MINAHASA SELATAN%')).count()

        print("="*60)
        print("HASIL SINKRONISASI MINAHASA SELATAN:")
        print(f"- Total Pengurus Minsel: {total_minsel}")
        print(f"- Total Pengurus DPD: {final_dpd_count} (Target: 34)")
        print(f"- Total DPC Aktif di DB: {len(distinct_dpcs)} (Target: 17)")
        print("DPC list:", [d[0] for d in distinct_dpcs])
        print("="*60)

if __name__ == '__main__':
    run_sync()
