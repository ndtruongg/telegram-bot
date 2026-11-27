"""Gemini API generator and curated fallback bank for daily English vocabulary."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

import requests

from bot.features.daily_vocab.models import (
    CONCRETE_TOPICS,
    TOPIC_LABELS_VI,
    VocabBatch,
    VocabItem,
    normalize_term,
)

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_MAX_PROMPT_EXCLUDES = 250

_TOPIC_PROMPT_GUIDANCE: dict[str, str] = {
    "it": (
        "Chủ đề: Tiếng Anh chuyên ngành IT / Kỹ thuật phần mềm (software engineering, "
        "system design, DevOps, debugging, code review, Agile/tech teamwork). "
        "Chọn các từ vựng, cụm từ hoặc thuật ngữ thực tế hay gặp trong tài liệu kỹ thuật, "
        "PR review và họp nhóm sản phẩm."
    ),
    "communication": (
        "Chủ đề: Tiếng Anh giao tiếp hằng ngày và công sở (workplace & daily English). "
        "Chọn các cụm động từ (phrasal verbs), thành ngữ (idioms) hoặc cách diễn đạt tự nhiên "
        "người bản xứ thường dùng khi trao đổi, thảo luận, đàm phán và trò chuyện hằng ngày."
    ),
    "ielts": (
        "Chủ đề: Từ vựng học thuật & luyện thi IELTS (mục tiêu Band 7.0–8.5 cho Speaking & Writing). "
        "Chọn các từ vựng nâng cao, collocations tự nhiên và cụm từ ghi điểm trong bài luận "
        "hoặc trả lời phỏng vấn IELTS."
    ),
}

FALLBACK_VOCAB_BANK: dict[str, tuple[VocabItem, ...]] = {
    "it": (
        VocabItem(
            term="bottleneck",
            phonetic="/ˈbɒt.əl.nek/",
            part_of_speech="n",
            meaning_vi="Điểm nghẽn hiệu năng, nút thắt cổ chai",
            example_en="Unindexed database queries became the main bottleneck during peak traffic.",
            example_vi="Các truy vấn cơ sở dữ liệu chưa đánh chỉ mục đã trở thành điểm nghẽn chính vào giờ cao điểm.",
            usage_note="Thường đi với: identify / resolve / eliminate a bottleneck.",
            topic="it",
        ),
        VocabItem(
            term="idempotent",
            phonetic="/ˌaɪ.demˈpoʊ.tənt/",
            part_of_speech="adj",
            meaning_vi="Lũy đẳng (gọi lặp lại nhiều lần vẫn cho cùng một kết quả)",
            example_en="Payment retry endpoints must be idempotent so customers are never charged twice.",
            example_vi="Các API thử lại thanh toán phải có tính lũy đẳng để khách hàng không bị trừ tiền hai lần.",
            usage_note="Rất phổ biến khi thiết kế REST API và hệ thống phân tán.",
            topic="it",
        ),
        VocabItem(
            term="technical debt",
            phonetic="/ˈtek.nɪ.kəl det/",
            part_of_speech="n phrase",
            meaning_vi="Nợ kỹ thuật (chi phí phải trả về sau do chọn giải pháp làm nhanh, thiếu chuẩn)",
            example_en="We dedicated this sprint to paying down technical debt in the authentication module.",
            example_vi="Chúng tôi dành sprint này để xử lý nợ kỹ thuật trong mô-đun xác thực.",
            usage_note="Collocation: accumulate / pay down / tackle technical debt.",
            topic="it",
        ),
        VocabItem(
            term="graceful degradation",
            phonetic="/ˈɡreɪs.fəl ˌdeɡ.rəˈdeɪ.ʃən/",
            part_of_speech="n phrase",
            meaning_vi="Suy giảm duyên dáng (hệ thống vẫn hoạt động phần cốt lõi khi một thành phần gặp lỗi)",
            example_en="With graceful degradation, the site still loads products even if the recommendation service is down.",
            example_vi="Nhờ cơ chế suy giảm duyên dáng, trang web vẫn tải được sản phẩm ngay cả khi dịch vụ gợi ý gặp sự cố.",
            usage_note="Khái niệm quan trọng trong thiết kế hệ thống chịu lỗi (fault tolerance).",
            topic="it",
        ),
        VocabItem(
            term="rollback",
            phonetic="/ˈroʊl.bæk/",
            part_of_speech="n / v",
            meaning_vi="Khôi phục về phiên bản ổn định trước đó",
            example_en="After error rates spiked, the on-call engineer triggered an immediate rollback.",
            example_vi="Sau khi tỷ lệ lỗi tăng vọt, kỹ sư trực sự cố đã kích hoạt khôi phục phiên bản ngay lập tức.",
            usage_note="Động từ tách: roll back a deployment / release.",
            topic="it",
        ),
        VocabItem(
            term="edge case",
            phonetic="/edʒ keɪs/",
            part_of_speech="n phrase",
            meaning_vi="Trường hợp biên, tình huống hiếm gặp nhưng dễ gây lỗi",
            example_en="Please add unit tests to cover edge cases like empty inputs and leap years.",
            example_vi="Vui lòng bổ sung unit test cho các trường hợp biên như dữ liệu rỗng và năm nhuận.",
            usage_note="Hay dùng trong code review và kiểm thử phần mềm.",
            topic="it",
        ),
        VocabItem(
            term="decouple",
            phonetic="/ˌdiːˈkʌp.əl/",
            part_of_speech="v",
            meaning_vi="Tách rời sự phụ thuộc giữa các thành phần hệ thống",
            example_en="Using a message queue helps decouple order processing from email notifications.",
            example_vi="Sử dụng hàng đợi tin nhắn giúp tách rời việc xử lý đơn hàng khỏi gửi email thông báo.",
            usage_note="Cấu trúc: decouple A from B.",
            topic="it",
        ),
        VocabItem(
            term="latency",
            phonetic="/ˈleɪ.tən.si/",
            part_of_speech="n",
            meaning_vi="Độ trễ (thời gian từ lúc gửi yêu cầu đến lúc nhận phản hồi)",
            example_en="Caching static assets at the CDN edge cut API latency by half.",
            example_vi="Lưu đệm tài nguyên tĩnh tại máy chủ biên CDN đã giảm một nửa độ trễ API.",
            usage_note="Phân biệt với throughput (thông lượng xử lý).",
            topic="it",
        ),
        VocabItem(
            term="deprecated",
            phonetic="/ˈdep.rə.keɪ.tɪd/",
            part_of_speech="adj",
            meaning_vi="Đã lỗi thời, không còn được khuyến khích sử dụng và sắp bị loại bỏ",
            example_en="The v1 endpoint is deprecated and will be removed next quarter.",
            example_vi="Endpoint v1 đã lỗi thời và sẽ bị gỡ bỏ vào quý tới.",
            usage_note="Hay xuất hiện trong changelog và tài liệu SDK.",
            topic="it",
        ),
        VocabItem(
            term="workaround",
            phonetic="/ˈwɜːrk.ə.raʊnd/",
            part_of_speech="n",
            meaning_vi="Giải pháp tạm thời để lách lỗi trước khi sửa triệt để",
            example_en="Clearing the browser cache is a temporary workaround until the patch ships.",
            example_vi="Xóa bộ nhớ đệm trình duyệt là giải pháp tạm thời cho đến khi bản vá được phát hành.",
            usage_note="Khác với permanent fix (bản sửa triệt để).",
            topic="it",
        ),
        VocabItem(
            term="scalability",
            phonetic="/ˌskeɪ.ləˈbɪl.ə.ti/",
            part_of_speech="n",
            meaning_vi="Khả năng mở rộng quy mô hệ thống khi tải tăng",
            example_en="Horizontal scalability allows us to add more servers as user traffic grows.",
            example_vi="Khả năng mở rộng theo chiều ngang cho phép chúng ta thêm máy chủ khi lưu lượng người dùng tăng.",
            usage_note="Horizontal scaling (thêm máy) vs. Vertical scaling (nâng cấu hình máy).",
            topic="it",
        ),
        VocabItem(
            term="root cause",
            phonetic="/ruːt kɔːz/",
            part_of_speech="n phrase",
            meaning_vi="Nguyên nhân gốc rễ của sự cố",
            example_en="The postmortem identified a misconfigured timeout as the root cause of the outage.",
            example_vi="Báo cáo phân tích sự cố xác định cấu hình timeout sai là nguyên nhân gốc rễ của việc gián đoạn.",
            usage_note="Cụm phổ biến: root cause analysis (RCA).",
            topic="it",
        ),
        VocabItem(
            term="boilerplate",
            phonetic="/ˈbɔɪ.lər.pleɪt/",
            part_of_speech="n",
            meaning_vi="Đoạn mã khuôn mẫu lặp đi lặp lại, ít thay đổi",
            example_en="Dataclasses in Python significantly reduce boilerplate code for model definitions.",
            example_vi="Dataclass trong Python giúp giảm đáng kể lượng mã khuôn mẫu khi định nghĩa model.",
            usage_note="Thường đi với: boilerplate code / reduce boilerplate.",
            topic="it",
        ),
        VocabItem(
            term="sanity check",
            phonetic="/ˈsæn.ə.ti tʃek/",
            part_of_speech="n phrase",
            meaning_vi="Kiểm tra nhanh tính hợp lý cơ bản trước khi đi sâu",
            example_en="Let's run a quick sanity check on the staging environment before releasing.",
            example_vi="Hãy chạy một lượt kiểm tra nhanh trên môi trường staging trước khi phát hành.",
            usage_note="Rất hay dùng trong giao tiếp nhóm dev hằng ngày.",
            topic="it",
        ),
        VocabItem(
            term="rate limiting",
            phonetic="/reɪt ˈlɪm.ɪ.tɪŋ/",
            part_of_speech="n phrase",
            meaning_vi="Giới hạn tần suất yêu cầu trong một khoảng thời gian",
            example_en="We enabled rate limiting on the login API to block brute-force attacks.",
            example_vi="Chúng tôi đã bật giới hạn tần suất trên API đăng nhập để chặn tấn công dò mật khẩu.",
            usage_note="Đi kèm mã lỗi HTTP 429 Too Many Requests.",
            topic="it",
        ),
    ),
    "communication": (
        VocabItem(
            term="touch base",
            phonetic="/tʌtʃ beɪs/",
            part_of_speech="idiom",
            meaning_vi="Liên hệ nhanh để cập nhật tình hình",
            example_en="Let's touch base tomorrow morning to review the client's feedback.",
            example_vi="Sáng mai chúng ta trao đổi nhanh để xem lại phản hồi của khách hàng nhé.",
            usage_note="Cấu trúc: touch base with someone (about something).",
            topic="communication",
        ),
        VocabItem(
            term="on the same page",
            phonetic="/ɒn ðə seɪm peɪdʒ/",
            part_of_speech="idiom",
            meaning_vi="Cùng chung cách hiểu, đồng thuận về mục tiêu",
            example_en="Before we start coding, let's make sure everyone is on the same page about the scope.",
            example_vi="Trước khi bắt đầu viết mã, hãy đảm bảo mọi người đều hiểu thống nhất về phạm vi công việc.",
            usage_note="Thường dùng: get / be / stay on the same page.",
            topic="communication",
        ),
        VocabItem(
            term="circle back",
            phonetic="/ˈsɜːr.kəl bæk/",
            part_of_speech="phrasal verb",
            meaning_vi="Quay lại thảo luận sau khi có thêm thông tin",
            example_en="I need to check the budget first; I'll circle back to you by Friday.",
            example_vi="Tôi cần kiểm tra ngân sách trước; tôi sẽ phản hồi lại bạn trước thứ Sáu.",
            usage_note="Rất lịch sự và tự nhiên trong email và cuộc họp công sở.",
            topic="communication",
        ),
        VocabItem(
            term="heads-up",
            phonetic="/ˌhedz ˈʌp/",
            part_of_speech="n",
            meaning_vi="Lời báo trước để ai đó kịp chuẩn bị",
            example_en="Thanks for the heads-up about the schedule change—I adjusted my afternoon plans.",
            example_vi="Cảm ơn bạn đã báo trước về việc đổi lịch—tôi đã điều chỉnh kế hoạch buổi chiều.",
            usage_note="Collocation: give someone a heads-up.",
            topic="communication",
        ),
        VocabItem(
            term="wrap up",
            phonetic="/ræp ʌp/",
            part_of_speech="phrasal verb",
            meaning_vi="Hoàn tất, kết thúc một cuộc họp hoặc công việc",
            example_en="We only have five minutes left, so let's wrap up and assign the action items.",
            example_vi="Chúng ta chỉ còn năm phút nữa, hãy kết thúc cuộc họp và phân công việc cần làm nhé.",
            usage_note="Dùng cho cả meeting, report, project.",
            topic="communication",
        ),
        VocabItem(
            term="follow up",
            phonetic="/ˈfɒl.oʊ ʌp/",
            part_of_speech="phrasal verb",
            meaning_vi="Theo dõi tiếp, nhắc lại hoặc hỏi cập nhật sau lần trao đổi trước",
            example_en="I'm writing to follow up on the proposal I sent last Tuesday.",
            example_vi="Tôi viết thư này để hỏi cập nhật về đề xuất tôi đã gửi hôm thứ Ba tuần trước.",
            usage_note="Danh từ viết có gạch nối: a follow-up email / call.",
            topic="communication",
        ),
        VocabItem(
            term="take it with a grain of salt",
            phonetic="/teɪk ɪt wɪð ə ɡreɪn əv sɔːlt/",
            part_of_speech="idiom",
            meaning_vi="Đón nhận thông tin một cách thận trọng, không tin hoàn toàn ngay lập tức",
            example_en="Take those early rumors with a grain of salt until the official announcement comes out.",
            example_vi="Hãy tiếp nhận những tin đồn ban đầu đó một cách thận trọng cho đến khi có thông báo chính thức.",
            usage_note="Anh-Anh hay dùng: a pinch of salt.",
            topic="communication",
        ),
        VocabItem(
            term="keep someone in the loop",
            phonetic="/kiːp ... ɪn ðə luːp/",
            part_of_speech="idiom",
            meaning_vi="Luôn cập nhật thông tin cho ai đó nắm tình hình",
            example_en="Please keep me in the loop if the vendor updates the delivery date.",
            example_vi="Vui lòng cập nhật cho tôi biết nếu nhà cung cấp thay đổi ngày giao hàng nhé.",
            usage_note="Trái nghĩa: out of the loop (không nắm thông tin).",
            topic="communication",
        ),
        VocabItem(
            term="break the ice",
            phonetic="/breɪk ði aɪs/",
            part_of_speech="idiom",
            meaning_vi="Phá vỡ bầu không khí ngượng ngùng ban đầu",
            example_en="She shared a funny travel story to break the ice at the start of the workshop.",
            example_vi="Cô ấy kể một câu chuyện du lịch hài hước để làm nóng bầu không khí đầu buổi hội thảo.",
            usage_note="Danh từ: an icebreaker (hoạt động làm quen).",
            topic="communication",
        ),
        VocabItem(
            term="bring up",
            phonetic="/brɪŋ ʌp/",
            part_of_speech="phrasal verb",
            meaning_vi="Đề cập, đưa một vấn đề ra thảo luận",
            example_en="I'm glad you brought up the onboarding timeline during the meeting.",
            example_vi="Tôi rất mừng vì bạn đã đề cập đến tiến độ hội nhập nhân sự mới trong cuộc họp.",
            usage_note="Cũng có nghĩa khác là nuôi dưỡng (bring up children).",
            topic="communication",
        ),
        VocabItem(
            term="call it a day",
            phonetic="/kɔːl ɪt ə deɪ/",
            part_of_speech="idiom",
            meaning_vi="Kết thúc công việc trong ngày, nghỉ thôi",
            example_en="We've finished all the priority tasks—let's call it a day and get some rest.",
            example_vi="Chúng ta đã xong hết các việc ưu tiên rồi—nghỉ hôm nay thôi và đi nghỉ ngơi nào.",
            usage_note="Dùng thân mật cuối buổi làm việc hoặc buổi học.",
            topic="communication",
        ),
        VocabItem(
            term="meet halfway",
            phonetic="/miːt ˌhæfˈweɪ/",
            part_of_speech="idiom",
            meaning_vi="Thỏa hiệp, mỗi bên nhượng bộ một chút để đạt thỏa thuận",
            example_en="Both teams agreed to meet halfway on the deadline so quality wouldn't suffer.",
            example_vi="Cả hai nhóm đều đồng ý nhượng bộ một chút về hạn chót để chất lượng không bị ảnh hưởng.",
            usage_note="Cấu trúc: meet someone halfway.",
            topic="communication",
        ),
        VocabItem(
            term="figure out",
            phonetic="/ˈfɪɡ.jər aʊt/",
            part_of_speech="phrasal verb",
            meaning_vi="Tìm ra lời giải, hiểu ra cách giải quyết",
            example_en="It took us an hour to figure out why the invoice total didn't match.",
            example_vi="Chúng tôi mất một giờ mới tìm ra lý do tại sao tổng hóa đơn không khớp.",
            usage_note="Cực kỳ phổ biến trong cả nói và viết không trang trọng.",
            topic="communication",
        ),
        VocabItem(
            term="play it by ear",
            phonetic="/pleɪ ɪt baɪ ɪər/",
            part_of_speech="idiom",
            meaning_vi="Tùy cơ ứng biến theo tình hình thực tế thay vì lên kế hoạch cứng",
            example_en="The weather forecast looks uncertain, so let's play it by ear this weekend.",
            example_vi="Dự báo thời tiết cuối tuần này khá thất thường, nên cứ tùy tình hình rồi tính nhé.",
            usage_note="Thường dùng trong kế hoạch du lịch, lịch trình linh hoạt.",
            topic="communication",
        ),
        VocabItem(
            term="put off",
            phonetic="/pʊt ɒf/",
            part_of_speech="phrasal verb",
            meaning_vi="Trì hoãn, dời lại sang thời điểm khác",
            example_en="Don't put off preparing your presentation until the night before.",
            example_vi="Đừng trì hoãn việc chuẩn bị bài thuyết trình đến tận đêm hôm trước.",
            usage_note="Cấu trúc: put off + V-ing / Noun (đồng nghĩa: postpone).",
            topic="communication",
        ),
    ),
    "ielts": (
        VocabItem(
            term="mitigate",
            phonetic="/ˈmɪt.ɪ.ɡeɪt/",
            part_of_speech="v",
            meaning_vi="Giảm nhẹ, làm dịu bớt tác động tiêu cực",
            example_en="Expanding public transit is an effective way to mitigate urban air pollution.",
            example_vi="Mở rộng giao thông công cộng là cách hiệu quả để giảm nhẹ ô nhiễm không khí đô thị.",
            usage_note="Collocation ghi điểm Writing Task 2: mitigate risks / effects / damage.",
            topic="ielts",
        ),
        VocabItem(
            term="ubiquitous",
            phonetic="/juːˈbɪk.wɪ.təs/",
            part_of_speech="adj",
            meaning_vi="Phổ biến khắp nơi, hiện diện ở mọi nơi",
            example_en="Smartphones have become ubiquitous in modern classrooms and workplaces.",
            example_vi="Điện thoại thông minh đã trở nên phổ biến khắp nơi trong lớp học và công sở hiện đại.",
            usage_note="Từ thay thế đắt giá cho 'very common' hoặc 'everywhere'.",
            topic="ielts",
        ),
        VocabItem(
            term="exacerbate",
            phonetic="/ɪɡˈzæs.ə.beɪt/",
            part_of_speech="v",
            meaning_vi="Làm trầm trọng thêm một vấn đề sẵn có",
            example_en="Unregulated urban sprawl can exacerbate traffic congestion and housing shortages.",
            example_vi="Sự mở rộng đô thị thiếu kiểm soát có thể làm trầm trọng thêm tình trạng kẹt xe và thiếu nhà ở.",
            usage_note="Thay thế nâng cao cho 'make worse'.",
            topic="ielts",
        ),
        VocabItem(
            term="viable",
            phonetic="/ˈvaɪ.ə.bəl/",
            part_of_speech="adj",
            meaning_vi="Khả thi, có thể thực hiện và duy trì lâu dài",
            example_en="Solar power is now an economically viable alternative to fossil fuels.",
            example_vi="Năng lượng mặt trời hiện là một lựa chọn thay thế khả thi về mặt kinh tế cho nhiên liệu hóa thạch.",
            usage_note="Collocation: a viable alternative / solution / option.",
            topic="ielts",
        ),
        VocabItem(
            term="detrimental",
            phonetic="/ˌdet.rɪˈmen.təl/",
            part_of_speech="adj",
            meaning_vi="Có hại, gây bất lợi đáng kể",
            example_en="Prolonged screen exposure before bedtime has a detrimental impact on sleep quality.",
            example_vi="Tiếp xúc màn hình kéo dài trước khi ngủ gây tác động bất lợi lên chất lượng giấc ngủ.",
            usage_note="Cấu trúc: be detrimental to something / have a detrimental impact on.",
            topic="ielts",
        ),
        VocabItem(
            term="foster",
            phonetic="/ˈfɒs.tər/",
            part_of_speech="v",
            meaning_vi="Thúc đẩy, nuôi dưỡng (sự phát triển, tinh thần, đổi mới)",
            example_en="Extracurricular clubs help foster teamwork and critical thinking among students.",
            example_vi="Các câu lạc bộ ngoại khóa giúp nuôi dưỡng tinh thần làm việc nhóm và tư duy phản biện ở học sinh.",
            usage_note="Collocation: foster innovation / creativity / a sense of community.",
            topic="ielts",
        ),
        VocabItem(
            term="pivotal",
            phonetic="/ˈpɪv.ə.təl/",
            part_of_speech="adj",
            meaning_vi="Then chốt, đóng vai trò quyết định",
            example_en="Early childhood education plays a pivotal role in shaping lifelong learning habits.",
            example_vi="Giáo dục mầm non đóng vai trò then chốt trong việc hình thành thói quen học tập suốt đời.",
            usage_note="Thay thế ấn tượng cho 'very important': play a pivotal role in.",
            topic="ielts",
        ),
        VocabItem(
            term="alleviate",
            phonetic="/əˈliː.vi.eɪt/",
            part_of_speech="v",
            meaning_vi="Làm giảm bớt (gánh nặng, khó khăn, nghèo đói)",
            example_en="Targeted subsidies can help alleviate the financial burden on low-income families.",
            example_vi="Các khoản trợ cấp đúng đối tượng có thể giúp giảm bớt gánh nặng tài chính cho gia đình thu nhập thấp.",
            usage_note="Collocation: alleviate poverty / stress / congestion / pressure.",
            topic="ielts",
        ),
        VocabItem(
            term="indispensable",
            phonetic="/ˌɪn.dɪˈspen.sə.bəl/",
            part_of_speech="adj",
            meaning_vi="Không thể thiếu được",
            example_en="Digital literacy has become an indispensable skill in today's job market.",
            example_vi="Kỹ năng số đã trở thành một năng lực không thể thiếu trên thị trường lao động ngày nay.",
            usage_note="Cấu trúc: indispensable to / for something.",
            topic="ielts",
        ),
        VocabItem(
            term="disparity",
            phonetic="/dɪˈspær.ə.ti/",
            part_of_speech="n",
            meaning_vi="Sự chênh lệch, bất bình đẳng rõ rệt",
            example_en="Governments should invest in rural schools to bridge the educational disparity.",
            example_vi="Chính phủ nên đầu tư vào trường học nông thôn để thu hẹp khoảng cách chênh lệch giáo dục.",
            usage_note="Collocation: income disparity / widen or bridge a disparity.",
            topic="ielts",
        ),
        VocabItem(
            term="curb",
            phonetic="/kɜːrb/",
            part_of_speech="v",
            meaning_vi="Kiềm chế, kiểm soát để ngăn gia tăng",
            example_en="Stricter regulations are needed to curb carbon emissions from heavy industry.",
            example_vi="Cần có các quy định chặt chẽ hơn để kiềm chế lượng khí thải carbon từ công nghiệp nặng.",
            usage_note="Collocation: curb inflation / emissions / spending.",
            topic="ielts",
        ),
        VocabItem(
            term="resilient",
            phonetic="/rɪˈzɪl.jənt/",
            part_of_speech="adj",
            meaning_vi="Kiên cường, có khả năng phục hồi nhanh sau khó khăn",
            example_en="Diversifying exports makes local economies more resilient to global downturns.",
            example_vi="Đa dạng hóa xuất khẩu giúp các nền kinh tế địa phương chống chịu tốt hơn trước suy thoái toàn cầu.",
            usage_note="Danh từ: resilience (khả năng chống chịu / phục hồi).",
            topic="ielts",
        ),
        VocabItem(
            term="double-edged sword",
            phonetic="/ˌdʌb.əl.edʒd ˈsɔːrd/",
            part_of_speech="n phrase",
            meaning_vi="Con dao hai lưỡi (vừa có lợi vừa tiềm ẩn tác hại)",
            example_en="Generative AI is a double-edged sword that boosts productivity while raising privacy concerns.",
            example_vi="AI tạo sinh là con dao hai lưỡi vừa tăng năng suất vừa làm dấy lên lo ngại về quyền riêng tư.",
            usage_note="Rất hợp cho mở bài hoặc thân bài IELTS Writing dạng Discuss Both Views.",
            topic="ielts",
        ),
        VocabItem(
            term="tangible",
            phonetic="/ˈtæn.dʒə.bəl/",
            part_of_speech="adj",
            meaning_vi="Hữu hình, rõ ràng và có thể đo đếm được",
            example_en="Regular exercise delivers tangible benefits for both physical and mental health.",
            example_vi="Tập thể dục đều đặn mang lại những lợi ích rõ rệt cho cả sức khỏe thể chất lẫn tinh thần.",
            usage_note="Collocation: tangible benefits / results / evidence.",
            topic="ielts",
        ),
        VocabItem(
            term="stem from",
            phonetic="/stem frɒm/",
            part_of_speech="phrasal verb",
            meaning_vi="Bắt nguồn từ, xuất phát từ nguyên nhân nào đó",
            example_en="Many workplace conflicts stem from unclear expectations rather than lack of effort.",
            example_vi="Nhiều xung đột nơi công sở bắt nguồn từ kỳ vọng không rõ ràng hơn là do thiếu nỗ lực.",
            usage_note="Cấu trúc học thuật tự nhiên thay cho 'come from'.",
            topic="ielts",
        ),
    ),
}


def _build_prompt(
    topic: str,
    count: int,
    exclude_terms: Sequence[str],
    request_count: int,
) -> str:
    topic_label = TOPIC_LABELS_VI.get(topic, topic)
    guidance = _TOPIC_PROMPT_GUIDANCE.get(
        topic,
        f"Chủ đề: {topic_label}. Chọn các từ vựng và cụm từ tiếng Anh hữu ích, thực tế.",
    )

    recent_excludes = [t.strip() for t in exclude_terms[-_MAX_PROMPT_EXCLUDES:] if t.strip()]
    if recent_excludes:
        exclude_block = (
            "\nDANH SÁCH TỪ/CỤM TỪ ĐÃ HỌC (TUYỆT ĐỐI KHÔNG lặp lại hoặc chọn biến thể gần giống):\n"
            + ", ".join(recent_excludes)
            + "\n"
        )
    else:
        exclude_block = ""

    return (
        "Bạn là giáo viên tiếng Anh chuyên nghiệp dạy học viên Việt Nam.\n"
        f"{guidance}\n\n"
        f"Hãy chọn {request_count} từ vựng/cụm từ tiếng Anh hữu ích, đa dạng (kết hợp cả từ đơn "
        "và cụm từ/collocation/phrasal verb/idiom tự nhiên, trình độ B1–C1) để học viên học hiệu quả "
        f"(hệ thống sẽ lọc và lấy {count} mục tốt nhất không trùng lịch sử).\n"
        f"{exclude_block}\n"
        "Trả về DUY NHẤT một JSON object hợp lệ theo đúng cấu trúc sau:\n"
        "{\n"
        '  "items": [\n'
        "    {\n"
        '      "term": "từ hoặc cụm từ tiếng Anh (viết thường tự nhiên, không đánh số)",\n'
        '      "phonetic": "phiên âm IPA chuẩn, ví dụ /ˈbɒt.əl.nek/",\n'
        '      "part_of_speech": "từ loại ngắn gọn: n, v, adj, adv, phrasal verb, idiom, collocation, n phrase...",\n'
        '      "meaning_vi": "nghĩa tiếng Việt ngắn gọn, rõ ràng, sát ngữ cảnh (tối đa ~18 từ)",\n'
        '      "example_en": "1 câu ví dụ tiếng Anh tự nhiên, thực tế đúng chủ đề, có dùng chính xác term",\n'
        '      "example_vi": "bản dịch tiếng Việt tự nhiên của câu ví dụ",\n'
        '      "usage_note": "1 mẹo dùng, collocation hoặc ghi chú phân biệt ngắn gọn bằng tiếng Việt (tối đa ~20 từ)"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Quy tắc bắt buộc:\n"
        "- Không dùng thẻ HTML hay ký tự Markdown (**bold**, *italic*, `code`) trong giá trị JSON.\n"
        "- Mỗi mục trong `items` phải khác biệt hoàn toàn nhau và KHÔNG trùng với danh sách đã học.\n"
        "- Ví dụ tiếng Anh phải tự nhiên, ngắn gọn (10–22 từ), sát ngữ cảnh thực tế."
    )


def _extract_gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    content = candidates[0].get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(chunks).strip()


def _parse_vocab_json(
    raw_text: str,
    *,
    topic: str,
    excluded_normalized: set[str],
    limit: int,
) -> list[VocabItem]:
    cleaned = _CODE_FENCE_RE.sub("", raw_text.strip()).strip()
    if not cleaned:
        return []

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return []

    if not isinstance(data, dict):
        return []

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return []

    items: list[VocabItem] = []
    seen_in_batch: set[str] = set()
    for entry in raw_items:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term") or "").strip()
        meaning_vi = str(entry.get("meaning_vi") or "").strip()
        example_en = str(entry.get("example_en") or "").strip()
        if not term or not meaning_vi or not example_en:
            continue

        norm = normalize_term(term)
        if not norm or norm in excluded_normalized or norm in seen_in_batch:
            continue

        seen_in_batch.add(norm)
        items.append(
            VocabItem(
                term=term,
                phonetic=str(entry.get("phonetic") or "").strip(),
                part_of_speech=str(entry.get("part_of_speech") or "").strip(),
                meaning_vi=meaning_vi,
                example_en=example_en,
                example_vi=str(entry.get("example_vi") or "").strip(),
                usage_note=str(entry.get("usage_note") or "").strip(),
                topic=topic,
            )
        )
        if len(items) >= limit:
            break

    return items


def build_fallback_batch(
    topic: str,
    *,
    count: int = 4,
    exclude_terms: Sequence[str] = (),
    reason: str = "",
) -> VocabBatch:
    """Select non-duplicate vocabulary items from the built-in curated bank."""
    resolved_topic = topic if topic in CONCRETE_TOPICS else "it"
    excluded_normalized = {normalize_term(t) for t in exclude_terms if normalize_term(t)}

    primary_bank = FALLBACK_VOCAB_BANK[resolved_topic]
    selected: list[VocabItem] = []
    seen_batch: set[str] = set()

    for item in primary_bank:
        norm = normalize_term(item.term)
        if norm in excluded_normalized or norm in seen_batch:
            continue
        seen_batch.add(norm)
        selected.append(item)
        if len(selected) >= count:
            break

    # If the topic's offline bank is exhausted, pick unseen entries from other topics before recycling
    if len(selected) < count:
        for other_topic in CONCRETE_TOPICS:
            if other_topic == resolved_topic:
                continue
            for item in FALLBACK_VOCAB_BANK[other_topic]:
                norm = normalize_term(item.term)
                if norm in excluded_normalized or norm in seen_batch:
                    continue
                seen_batch.add(norm)
                selected.append(item)
                if len(selected) >= count:
                    break
            if len(selected) >= count:
                break

    # Last resort: recycle oldest entries from primary bank if every offline word was learned
    if len(selected) < count:
        for item in primary_bank:
            norm = normalize_term(item.term)
            if norm in seen_batch:
                continue
            seen_batch.add(norm)
            selected.append(item)
            if len(selected) >= count:
                break

    return VocabBatch(
        topic=resolved_topic,
        items=tuple(selected[:count]),
        used_ai=False,
        fallback_reason=reason,
    )


def generate_vocab_with_gemini(
    *,
    topic: str,
    count: int = 4,
    exclude_terms: Sequence[str] = (),
    api_key: str = "",
    model: str = "gemini-2.5-flash",
    timeout: int = 25,
    session: requests.Session | None = None,
) -> VocabBatch:
    """Generate unique English vocabulary via Gemini REST API, falling back to curated bank on error."""
    resolved_topic = topic if topic in CONCRETE_TOPICS else "it"
    target_count = max(1, min(10, count))

    if not api_key.strip():
        logger.info("Chưa cấu hình GEMINI_API_KEY, dùng kho từ vựng dự phòng.")
        return build_fallback_batch(
            resolved_topic,
            count=target_count,
            exclude_terms=exclude_terms,
            reason="Chưa cấu hình GEMINI_API_KEY",
        )

    excluded_normalized = {normalize_term(t) for t in exclude_terms if normalize_term(t)}
    request_count = min(target_count + 2, 10)
    prompt = _build_prompt(
        topic=resolved_topic,
        count=target_count,
        exclude_terms=exclude_terms,
        request_count=request_count,
    )
    endpoint = f"{GEMINI_API_BASE}/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            "responseMimeType": "application/json",
        },
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key.strip(),
    }

    client = session or requests
    try:
        response = client.post(endpoint, headers=headers, json=body, timeout=timeout)
        if response.status_code != 200:
            logger.warning(
                "Gemini API trả về lỗi (%s): %s",
                response.status_code,
                response.text[:300],
            )
            return build_fallback_batch(
                resolved_topic,
                count=target_count,
                exclude_terms=exclude_terms,
                reason=f"Gemini API lỗi HTTP {response.status_code}",
            )

        payload = response.json()
        raw_text = _extract_gemini_text(payload)
        parsed_items = _parse_vocab_json(
            raw_text,
            topic=resolved_topic,
            excluded_normalized=excluded_normalized,
            limit=target_count,
        )
        if parsed_items:
            if len(parsed_items) < target_count:
                # Top up any remaining slots from unseen curated entries so user still gets full count
                extra_excludes = list(exclude_terms) + [item.term for item in parsed_items]
                topup = build_fallback_batch(
                    resolved_topic,
                    count=target_count - len(parsed_items),
                    exclude_terms=extra_excludes,
                )
                parsed_items.extend(topup.items)
            return VocabBatch(
                topic=resolved_topic,
                items=tuple(parsed_items[:target_count]),
                used_ai=True,
            )

        logger.warning(
            "Không thể phân tích JSON hợp lệ hoặc toàn bộ từ bị trùng lịch sử, chuyển sang kho dự phòng."
        )
        return build_fallback_batch(
            resolved_topic,
            count=target_count,
            exclude_terms=exclude_terms,
            reason="Phản hồi AI không hợp lệ hoặc trùng lịch sử",
        )
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Lỗi gọi Gemini API (%s), chuyển sang kho từ vựng dự phòng.", exc)
        return build_fallback_batch(
            resolved_topic,
            count=target_count,
            exclude_terms=exclude_terms,
            reason="Không kết nối được Gemini API",
        )
