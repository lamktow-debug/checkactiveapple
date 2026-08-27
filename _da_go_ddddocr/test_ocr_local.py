"""Test cho lớp gọi OCR máy.

Không cần cài ddddocr: dùng worker giả nói đúng giao thức, nhưng vẫn chạy tiến
trình con thật nên phần ống dẫn được kiểm tra thật sự.
"""

import base64
import json
import os
import stat
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ocr_local import (
    MIN_HIT_RATE,
    LocalOcr,
    LocalOcrUnavailable,
    normalize_code,
    strip_data_uri,
    self_test,
    summarize,
)

ANH = base64.b64encode(b"anh-captcha-gia").decode()


def viet_worker(thu_muc, than_vong_lap, khoi_dong='print(\'{"ready": true}\', flush=True)'):
    """Tạo một worker giả bằng Python, nói đúng giao thức của worker thật."""
    path = Path(thu_muc) / "worker_gia.py"
    path.write_text(textwrap.dedent(f"""
        import json, sys
        {khoi_dong}
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            {than_vong_lap}
    """).strip() + "\n", encoding="utf-8")
    return path


class ChuanHoaTests(unittest.TestCase):
    def test_giu_dung_chu_va_so_viet_hoa(self):
        self.assertEqual(normalize_code(" qw7t "), "QW7T")
        self.assertEqual(normalize_code("a-b_c 1"), "ABC1")
        self.assertEqual(normalize_code(""), "")
        self.assertEqual(normalize_code(None), "")

    def test_bo_tien_to_data_uri(self):
        self.assertEqual(strip_data_uri("data:image/png;base64,QUJD"), "QUJD")
        self.assertEqual(strip_data_uri("QUJD"), "QUJD")
        self.assertEqual(strip_data_uri(None), "")


class GiaiCaptchaTests(unittest.TestCase):
    """Chạy tiến trình con thật, chỉ thay phần model bằng worker giả."""

    def _ocr(self, thu_muc, than, khoi_dong=None):
        kwargs = {} if khoi_dong is None else {"khoi_dong": khoi_dong}
        worker = viet_worker(thu_muc, than, **kwargs)
        return LocalOcr(python_path=sys.executable, worker_path=worker)

    def test_giai_duoc_va_tra_ma_da_chuan_hoa(self):
        with TemporaryDirectory() as tmp:
            ocr = self._ocr(tmp, 'print(json.dumps({"code": " qw7t "}), flush=True)')
            try:
                self.assertEqual(ocr.solve(ANH), "QW7T")
                self.assertEqual(ocr.attempts, 1)
            finally:
                ocr.close()

    def test_dung_lai_tien_trinh_cho_nhieu_lan_giai(self):
        """Model 76MB chi duoc nap mot lan, khong mo lai moi anh."""
        with TemporaryDirectory() as tmp:
            ocr = self._ocr(tmp, 'print(json.dumps({"code": "ABCD"}), flush=True)')
            try:
                ocr.solve(ANH)
                pid_dau = ocr._process.pid
                ocr.solve(ANH)
                ocr.solve(ANH)
                self.assertEqual(ocr._process.pid, pid_dau)
                self.assertEqual(ocr.attempts, 3)
            finally:
                ocr.close()

    def test_anh_co_tien_to_data_uri_van_giai_duoc(self):
        with TemporaryDirectory() as tmp:
            ocr = self._ocr(tmp, 'print(json.dumps({"code": "WXYZ"}), flush=True)')
            try:
                self.assertEqual(ocr.solve("data:image/png;base64," + ANH), "WXYZ")
            finally:
                ocr.close()


class HongThiQuayVe2CaptchaTests(unittest.TestCase):
    """Moi kieu hong deu phai ra None, khong duoc nem loi lam sap ca me chay."""

    def test_chua_cai_venv_ocr(self):
        ocr = LocalOcr(python_path="/khong/co/python", worker_path="/khong/co/worker.py")
        self.assertFalse(ocr.installed)
        self.assertIsNone(ocr.solve(ANH))
        self.assertIn("chua co venv-ocr", ocr.broken_reason)

    def test_worker_bao_thieu_ddddocr(self):
        with TemporaryDirectory() as tmp:
            worker = viet_worker(
                tmp, "pass",
                khoi_dong='print(\'{"error": "chua cai ddddocr"}\', flush=True); sys.exit(1)')
            ocr = LocalOcr(python_path=sys.executable, worker_path=worker)
            self.assertIsNone(ocr.solve(ANH))
            self.assertIn("ddddocr", ocr.broken_reason)

    def test_worker_in_ra_rac_luc_khoi_dong(self):
        with TemporaryDirectory() as tmp:
            worker = viet_worker(tmp, "pass",
                                 khoi_dong='print("quang cao lung tung", flush=True)')
            ocr = LocalOcr(python_path=sys.executable, worker_path=worker)
            self.assertIsNone(ocr.solve(ANH))
            self.assertIn("rac", ocr.broken_reason)

    def test_worker_tra_ve_loi_thi_bo_qua_anh_do(self):
        with TemporaryDirectory() as tmp:
            worker = viet_worker(tmp, 'print(json.dumps({"error": "anh hong"}), flush=True)')
            ocr = LocalOcr(python_path=sys.executable, worker_path=worker)
            try:
                self.assertIsNone(ocr.solve(ANH))
                self.assertIsNone(ocr.broken_reason, "loi mot anh khong phai la hong han")
            finally:
                ocr.close()

    def test_worker_tra_ma_rong(self):
        with TemporaryDirectory() as tmp:
            worker = viet_worker(tmp, 'print(json.dumps({"code": "   "}), flush=True)')
            ocr = LocalOcr(python_path=sys.executable, worker_path=worker)
            try:
                self.assertIsNone(ocr.solve(ANH))
                self.assertEqual(ocr.attempts, 0, "ma rong khong duoc tinh la mot lan doan")
            finally:
                ocr.close()

    def test_worker_treo_thi_bo_qua_theo_han_gio(self):
        import ocr_local

        cu = ocr_local.SOLVE_TIMEOUT_SECONDS
        ocr_local.SOLVE_TIMEOUT_SECONDS = 0.5
        try:
            with TemporaryDirectory() as tmp:
                worker = viet_worker(tmp, "import time; time.sleep(30)")
                ocr = LocalOcr(python_path=sys.executable, worker_path=worker)
                try:
                    self.assertIsNone(ocr.solve(ANH))
                finally:
                    ocr.close()
        finally:
            ocr_local.SOLVE_TIMEOUT_SECONDS = cu

    def test_worker_chet_giua_chung_thi_tu_mo_lai(self):
        with TemporaryDirectory() as tmp:
            worker = viet_worker(tmp, 'print(json.dumps({"code": "ABCD"}), flush=True)')
            ocr = LocalOcr(python_path=sys.executable, worker_path=worker)
            try:
                self.assertEqual(ocr.solve(ANH), "ABCD")
                ocr._process.kill()
                ocr._process.wait()
                self.assertEqual(ocr.solve(ANH), "ABCD", "phai tu mo lai worker")
            finally:
                ocr.close()


class ThongKeTests(unittest.TestCase):
    def test_ti_le_tinh_tren_MOI_lan_goi_ke_ca_lan_doc_hut(self):
        """Lan doc hut cung ton dung ngan ay thoi gian, phai nam trong mau so."""
        ocr = LocalOcr(python_path="/khong/co", worker_path="/khong/co")
        self.assertIsNone(ocr.accuracy())
        ocr.tries, ocr.accepted = 10, 7
        self.assertAlmostEqual(ocr.accuracy(), 0.7)

        # 7 trung / 10 lan goi = 70%, KHONG phai 7/7 = 100%
        ocr.tries, ocr.accepted, ocr.wrong_length = 10, 7, 3
        self.assertAlmostEqual(ocr.accuracy(), 0.7)

    def test_cau_tong_ket(self):
        ocr = LocalOcr(python_path="/khong/co", worker_path="/khong/co")
        self.assertIsNone(summarize(ocr), "chua doan lan nao thi khong noi gi")
        self.assertIsNone(summarize(None))
        ocr.tries, ocr.accepted = 18, 12
        cau = summarize(ocr)
        self.assertIn("12/18", cau)
        self.assertIn("67%", cau)


class SelfTestTests(unittest.TestCase):
    def test_bao_san_sang_khi_worker_tra_loi_ping(self):
        with TemporaryDirectory() as tmp:
            worker = viet_worker(
                tmp,
                'print(json.dumps({"code": "PONG" if line == "PING" else "X"}), flush=True)')
            ok, message = self_test(sys.executable, worker)
            self.assertTrue(ok, message)

    def test_bao_thieu_khi_chua_cai(self):
        ok, message = self_test("/khong/co/python", "/khong/co/worker.py")
        self.assertFalse(ok)
        self.assertIn("Chua co", message)


class ChonUngVienTests(unittest.TestCase):
    """Captcha Apple luon 4 ky tu — phai lay ung vien dung do dai."""

    class _ModelGia:
        def __init__(self, cac_ket_qua):
            self.cac_ket_qua = list(cac_ket_qua)
            self.so_lan = 0

        def classification(self, _anh):
            self.so_lan += 1
            if self.cac_ket_qua:
                return self.cac_ket_qua.pop(0)
            return ""

    def setUp(self):
        import ocr_worker

        self.worker = ocr_worker
        # Khoa lai mot bien the duy nhat de dem so lan goi cho de hieu
        self._cac_bien_the = ocr_worker.cac_bien_the_anh
        ocr_worker.cac_bien_the_anh = lambda raw, scale=3: [
            ("goc", raw), ("phong-to", raw), ("den-trang", raw)]

    def tearDown(self):
        self.worker.cac_bien_the_anh = self._cac_bien_the

    def test_bo_qua_ma_3_ky_tu_va_lay_ma_4_ky_tu(self):
        model = self._ModelGia(["QW7", "QW7", "QW7T"])
        code, ung_vien, khop = self.worker.doan([model], b"anh", 4)
        self.assertEqual(code, "QW7T")
        self.assertTrue(khop)
        self.assertEqual(len(ung_vien), 3, "phai thu tiep sau khi doc hut")

    def test_dung_ngay_khi_ban_dau_da_dung_do_dai(self):
        model = self._ModelGia(["QW7T", "KHONG-DUNG-TOI"])
        code, ung_vien, khop = self.worker.doan([model], b"anh", 4)
        self.assertEqual(code, "QW7T")
        self.assertEqual(model.so_lan, 1, "dung roi thi khong thu them cho ton thoi gian")

    def test_thu_ca_hai_model(self):
        model_a = self._ModelGia(["ABC", "ABC", "ABC"])
        model_b = self._ModelGia(["WXYZ"])
        code, _, khop = self.worker.doan([model_a, model_b], b"anh", 4)
        self.assertEqual(code, "WXYZ")
        self.assertTrue(khop, "model beta doc dung thi phai dung ket qua do")

    def test_khong_cai_nao_dung_thi_bao_matched_false(self):
        model = self._ModelGia(["AB", "ABC", "AB"])
        code, ung_vien, khop = self.worker.doan([model], b"anh", 4)
        self.assertFalse(khop)
        self.assertEqual(code, "ABC", "tra ve cai dai nhat de con biet duong")
        self.assertEqual(len(ung_vien), 3)

    def test_model_nem_loi_thi_bo_qua_chu_khong_sap(self):
        class Hong:
            def classification(self, _a):
                raise RuntimeError("model hong")

        code, _, khop = self.worker.doan([Hong(), self._ModelGia(["QW7T"])], b"anh", 4)
        self.assertEqual(code, "QW7T")

    def test_do_dai_0_thi_chap_nhan_moi_thu(self):
        model = self._ModelGia(["AB"])
        code, _, khop = self.worker.doan([model], b"anh", 0)
        self.assertEqual(code, "AB")
        self.assertTrue(khop)


class XuLyAnhTests(unittest.TestCase):
    """Phong to, tang tuong phan, nhi phan hoa — de model khoi nuot ky tu."""

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("may nay khong co Pillow")

    def _anh_that(self):
        import io

        from PIL import Image, ImageDraw

        anh = Image.new("RGB", (120, 40), "white")
        ve = ImageDraw.Draw(anh)
        ve.text((12, 12), "QW7T", fill="black")
        ve.line((0, 30, 120, 12), fill="gray")   # net nhieu vat ngang
        buffer = io.BytesIO()
        anh.save(buffer, format="PNG")
        return buffer.getvalue()

    def test_sinh_ra_nhieu_bien_the_va_ban_goc_dung_dau(self):
        import ocr_worker

        bien_the = ocr_worker.cac_bien_the_anh(self._anh_that())
        ten = [t for t, _ in bien_the]
        self.assertEqual(ten[0], "goc", "ban goc phai thu truoc cho nhanh")
        self.assertGreaterEqual(len(bien_the), 4, f"chi sinh duoc {ten}")
        self.assertIn("den-trang", ten)

    def test_bien_the_deu_la_anh_mo_duoc_va_da_phong_to(self):
        import io

        import ocr_worker
        from PIL import Image

        goc = self._anh_that()
        for ten, data in ocr_worker.cac_bien_the_anh(goc):
            anh = Image.open(io.BytesIO(data))
            anh.load()
            if ten != "goc":
                self.assertGreater(anh.size[0], 120, f"{ten} chua duoc phong to")

    def test_anh_hong_thi_van_tra_ve_ban_goc(self):
        import ocr_worker

        bien_the = ocr_worker.cac_bien_the_anh(b"day khong phai anh")
        self.assertEqual([t for t, _ in bien_the], ["goc"])


class DoDaiMaTests(unittest.TestCase):
    """Client phai chan ma sai do dai truoc khi no kip bay len Apple."""

    def _ocr_tra(self, payload):
        # Giu thu muc tam song den het test: xoa som thi worker khong con file
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worker = viet_worker(tmp.name, f"print(json.dumps({payload!r}), flush=True)")
        return LocalOcr(python_path=sys.executable, worker_path=worker)

    def test_ma_3_ky_tu_bi_tu_choi(self):
        ocr = self._ocr_tra({"code": "QW7", "matched": False, "candidates": ["goc/m0:QW7"]})
        try:
            self.assertIsNone(ocr.solve(ANH, expected_length=4))
            self.assertEqual(ocr.wrong_length, 1)
            self.assertEqual(ocr.attempts, 0, "doc hut khong duoc tinh la mot lan doan")
        finally:
            ocr.close()

    def test_ma_4_ky_tu_duoc_nhan(self):
        ocr = self._ocr_tra({"code": "QW7T", "matched": True, "candidates": []})
        try:
            self.assertEqual(ocr.solve(ANH, expected_length=4), "QW7T")
            self.assertEqual(ocr.wrong_length, 0)
            self.assertEqual(ocr.attempts, 1)
        finally:
            ocr.close()

    def test_ma_5_ky_tu_cung_bi_tu_choi(self):
        ocr = self._ocr_tra({"code": "QW7TX", "matched": False, "candidates": []})
        try:
            self.assertIsNone(ocr.solve(ANH, expected_length=4))
        finally:
            ocr.close()

    def test_giu_lai_cac_ung_vien_de_con_soi(self):
        ocr = self._ocr_tra({"code": "QW7", "matched": False,
                             "candidates": ["goc/m0:QW7", "den-trang/m1:QW"]})
        try:
            ocr.solve(ANH, expected_length=4)
            self.assertEqual(len(ocr.last_candidates), 2)
        finally:
            ocr.close()

    def test_tong_ket_khuyen_tat_khi_duoi_muc_hoa_von(self):
        ocr = LocalOcr(python_path="/khong/co", worker_path="/khong/co")
        ocr.tries, ocr.accepted, ocr.wrong_length = 10, 2, 8
        cau = summarize(ocr)
        self.assertIn("2/10", cau)
        self.assertIn("8 lần đọc thiếu ký tự", cau)
        self.assertIn("nên tắt", cau)

    def test_tong_ket_khong_khuyen_tat_khi_dang_co_lai(self):
        ocr = LocalOcr(python_path="/khong/co", worker_path="/khong/co")
        ocr.tries, ocr.accepted = 10, 9
        cau = summarize(ocr)
        self.assertNotIn("nên tắt", cau)


class CongTuTatTests(unittest.TestCase):
    """Do that 26/08: 18 serial mat 10 phut voi OCR may, 6 phut voi 2captcha.

    Truot nhieu qua thi OCR may phai TU RUT, khong bat nguoi dung ngoi doan.
    """

    def _ocr(self, tries=0, accepted=0):
        ocr = LocalOcr(python_path="/khong/co", worker_path="/khong/co")
        ocr.tries, ocr.accepted = tries, accepted
        return ocr

    def test_chua_du_du_lieu_thi_chua_ket_luan(self):
        import ocr_local

        ocr = self._ocr(tries=ocr_local.WARMUP_TRIES - 1, accepted=0)
        self.assertIsNone(ocr.should_give_up(),
                          "phai cho chay du so lan khoi dong roi moi ket luan")

    def test_truot_nhieu_thi_bo_cuoc(self):
        import ocr_local

        ocr = self._ocr(tries=ocr_local.WARMUP_TRIES, accepted=0)
        ly_do = ocr.should_give_up()
        self.assertIsNotNone(ly_do)
        self.assertIn("dưới mức hoà vốn", ly_do)

    def test_trung_nhieu_thi_chay_tiep(self):
        import ocr_local

        n = ocr_local.WARMUP_TRIES * 2
        ocr = self._ocr(tries=n, accepted=n)  # trung 100%
        self.assertIsNone(ocr.should_give_up())

    def test_ngay_tren_nguong_thi_van_chay(self):
        import ocr_local

        n = 20
        ocr = self._ocr(tries=n, accepted=int(n * ocr_local.MIN_HIT_RATE) + 1)
        self.assertIsNone(ocr.should_give_up())

    def test_bo_cuoc_roi_thi_khong_goi_OCR_nua(self):
        """Bo cuoc phai la that: khong duoc dong tien trinh con nao nua."""
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worker = viet_worker(tmp.name,
                             'print(json.dumps({"code": "QW7T"}), flush=True)')
        ocr = LocalOcr(python_path=sys.executable, worker_path=worker)
        try:
            ocr.gave_up_reason = "da tat"
            self.assertIsNone(ocr.solve(ANH))
            self.assertIsNone(ocr._process, "khong duoc mo worker khi da bo cuoc")
            self.assertEqual(ocr.tries, 0, "bo cuoc roi thi khong tinh them lan nao")
        finally:
            ocr.close()

    def test_tu_bo_cuoc_ngay_trong_luc_chay(self):
        """Chay that: worker luon doc hut, den lan thu WARMUP thi phai tu tat."""
        import ocr_local

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        worker = viet_worker(
            tmp.name,
            'print(json.dumps({"code": "QW7", "matched": False}), flush=True)')
        ocr = LocalOcr(python_path=sys.executable, worker_path=worker)
        try:
            for _ in range(ocr_local.WARMUP_TRIES + 3):
                ocr.solve(ANH, expected_length=4)
            self.assertIsNotNone(ocr.gave_up_reason, "phai tu tat sau khi truot nhieu")
            self.assertLessEqual(
                ocr.tries, ocr_local.WARMUP_TRIES,
                "khong duoc goi them sau khi da bo cuoc")
        finally:
            ocr.close()


if __name__ == "__main__":
    unittest.main()
