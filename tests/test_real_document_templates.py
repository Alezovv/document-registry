import unittest

from app.document_parser import DocumentParser


class RealDocumentTemplateTests(unittest.TestCase):
    def setUp(self):
        self.parser = DocumentParser()

    def test_hospital_serum_letter_uses_tail_as_subject_not_person(self):
        filename = (
            "ГУЗ КБСМП №15 № 3200 от 15.06.2026г. - "
            "сыворотка противостолбнячная.pdf"
        )
        meta = DocumentParser.extract_from_filename(filename)
        self.assertEqual(meta["from_department"], "ГУЗ КБСМП №15")
        self.assertEqual(meta["received_date"], "15.06.2026")
        self.assertEqual(meta["to_person"], "")
        self.assertEqual(meta["request_subject"], "Сыворотка противостолбнячная")

    def test_hospital_stray_animals_letter_does_not_put_recipient_org_into_person(self):
        filename = (
            "ГУЗ КБСМП №15 № 4345 от 11.08.2026г. - МБУ Северное - "
            "об отлове беспризорных животных.pdf"
        )
        meta = DocumentParser.extract_from_filename(filename)
        self.assertEqual(meta["from_department"], "ГУЗ КБСМП №15")
        self.assertEqual(meta["to_person"], "")
        self.assertEqual(meta["request_subject"], "Об отлове беспризорных животных")

    def test_incoming_stamp_beats_outgoing_court_date(self):
        text = """
ГУЗ КБСМП №15 Вх. № 3967 10 06 2026
Красноармейский районный суд города Волгограда
08.06.2026 № 2-1850/2026
В производстве Красноармейского районного суда находится исковое заявление.
В целях своевременного рассмотрения дела прошу Вас направить в адрес суда
медицинскую карту больного и всю имеющуюся документацию в отношении
несовершеннолетней Толстенко Алисы Витальевны, 06 октября 2021 года рождения.
Судья /подпись/
Документ подписан электронной подписью
Владелец Городилова Татьяна Олеговна
Исп. помощник судьи М.В. Лепко
"""
        filename = (
            "Письмо - запрос Красноармейский районный суд г. Волгограда "
            "от 08.06.2026г. № 2-1850-2026 Городилова Т.О. - Толстенко А.В..pdf"
        )
        request = self.parser.parse(text, file_name=filename)
        self.assertEqual(request.received_date, "10.06.2026")
        self.assertEqual(request.from_department, "Красноармейский районный суд г. Волгограда")
        self.assertEqual(request.from_person, "Городилова Т.О.")
        self.assertEqual(request.to_person, "Толстенко А.В.")
        self.assertIn("медицинскую карту", request.request_subject.lower())

    def test_traffic_police_letter_uses_incoming_stamp_and_filename_people(self):
        text = """
ГУЗ КБСМП №15 Вх. 3828 24 06 2026
УПРАВЛЕНИЕ МВД РОССИИ ПО ГОРОДУ ВОЛГОГРАДУ
ОБДПС ГИБДД УМВД России по г. Волгограду
23.06.26
В связи со служебной необходимостью прошу выслать почтой в десятидневный срок
с момента получения данного запроса: историю болезни (мед. карту), амбулаторную
карту, рентген снимки, диск-КТ с указанием телесных повреждений
Мамедовой Арзу Ислам кызы 14.12.2006 г.р.
С уважением,
Инспектор ИАЗ                       Темерёва Ю.В.
"""
        filename = (
            "Письмо - запрос ОБДПС ГИБДД УМВД России по г.Волгограду "
            "от 23.06.2026г. Темерева Ю.В. - Мамедова А.И.к.pdf"
        )
        request = self.parser.parse(text, file_name=filename)
        self.assertEqual(request.received_date, "24.06.2026")
        self.assertEqual(request.from_department, "ОБДПС ГИБДД УМВД России по г.Волгограду")
        self.assertEqual(request.from_person, "Темерева Ю.В.")
        self.assertEqual(request.to_person, "Мамедова А.И.")
        self.assertIn("выслать почтой", request.request_subject.lower())

    def test_omvd_letter_extracts_medical_request_subject(self):
        text = """
ГУЗ КБСМП №15 Вх. № 4880 06 08 2026
ГУ МВД РОССИИ ПО ВОЛГОГРАДСКОЙ ОБЛАСТИ
ОТДЕЛ МВД РОССИИ ПО СВЕТЛОЯРСКОМУ РАЙОНУ
Запрос
В связи с проведением процессуальной проверки по материалу прошу Вас поручить
вверенным Вам сотрудникам, выдать медицинскую документацию на гр. Сомову
Геннадию Ивановичу, 11.09.1963 г.р.
С уважением,
УУП ОУУП и ПДН ОМВД России по Светлоярскому району
Волгоградской области лейтенант полиции                   В.А. Штерц
"""
        filename = (
            "Письмо - запрос ОМВД РОссии по Светлоярскому р-ну "
            "Штерц В.А. - Сомов Г.И..pdf"
        )
        request = self.parser.parse(text, file_name=filename)
        self.assertEqual(request.received_date, "06.08.2026")
        self.assertEqual(request.from_department, "ОМВД РОссии по Светлоярскому р-ну")
        self.assertEqual(request.from_person, "Штерц В.А.")
        self.assertEqual(request.to_person, "Сомов Г.И.")
        self.assertIn("выдать медицинскую документацию", request.request_subject.lower())

    def test_spatial_incoming_stamp_date_is_found_across_neighbor_blocks(self):
        data = {
            "text": "Вх. № 3828\n24 06 2026\n23.06.2026",
            "blocks": [
                {"text": "Вх. № 3828", "x1": 20, "y1": 15, "x2": 130, "y2": 35},
                {"text": "24 06 2026", "x1": 40, "y1": 38, "x2": 145, "y2": 58},
                {"text": "23.06.2026", "x1": 200, "y1": 150, "x2": 300, "y2": 170},
            ],
        }
        request = self.parser.parse(data)
        self.assertEqual(request.received_date, "24.06.2026")

    def test_hospital_letterhead_never_becomes_subject_person(self):
        request = self.parser.parse(
            """КЛИНИЧЕСКАЯ БОЛЬНИЦА СКОРОЙ МЕДИЦИНСКОЙ ПОМОЩИ №15
Администрация больницы просит Вас перераспределить сыворотку противостолбнячную.
Главный врач А.А. Калмыков
""",
            file_name="ГУЗ КБСМП №15 - сыворотка противостолбнячная.pdf",
        )
        self.assertEqual(request.to_person, "")



if __name__ == "__main__":
    unittest.main()
