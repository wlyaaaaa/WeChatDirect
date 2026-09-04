from __future__ import annotations

import unittest

from wechat_render import render_conversation_html


class WeChatRenderTests(unittest.TestCase):
    def test_messages_keep_input_order_escape_text_and_show_direction_and_time(self):
        html = render_conversation_html(
            account="primary",
            contact={"displayName": "测试联系人"},
            messages=[
                {
                    "serverId": 1,
                    "createTime": 100,
                    "sortSeq": 1,
                    "content": "先出现\n第二行",
                    "sender": {"displayName": "<对方>", "role": "other"},
                    "direction": "incoming",
                },
                {
                    "serverId": 2,
                    "createTime": 110,
                    "sortSeq": 2,
                    "content": "<script>alert('x')</script>",
                    "sender": {"displayName": "我", "role": "self"},
                    "direction": "outgoing",
                },
            ],
        )

        self.assertLess(html.index("先出现"), html.index("&lt;script&gt;"))
        self.assertIn("&lt;对方&gt;", html)
        self.assertNotIn("<script>alert", html)
        self.assertIn("message-incoming", html)
        self.assertIn("message-outgoing", html)
        self.assertIn("收到", html)
        self.assertIn("发送", html)
        self.assertIn("1970-01-01 08:01:40", html)
        self.assertIn("data-sort-seq=\"1\"", html)
        self.assertIn("white-space: pre-wrap", html)

    def test_media_occurrences_render_in_place_and_voice_prefers_wav(self):
        html = render_conversation_html(
            account="primary",
            contact={"displayName": "媒体联系人"},
            messages=[
                {
                    "serverId": 7,
                    "createTime": 100,
                    "content": "媒体顺序",
                    "sender": {"displayName": "对方", "role": "other"},
                    "media_manifest": [
                        {
                            "kind": "emoji",
                            "exportedPath": "media/sticker.gif",
                            "quality": "thumbnail",
                        },
                        {"kind": "emoji", "exportedPath": "media/sticker.gif"},
                        {
                            "kind": "voice",
                            "exportedPath": "media/voice.silk",
                            "exportStatus": "available_local",
                            "derivedVoiceWav": {"path": "media/voice.wav"},
                        },
                        {"kind": "video", "exportedPath": "media/clip.mp4"},
                        {"kind": "file", "exportedPath": "media/readme.pdf"},
                    ],
                }
            ],
        )

        self.assertIn('<img class="media-image"', html)
        self.assertIn('src="media/sticker.gif"', html)
        self.assertEqual(html.count('data-media-index="0"'), 1)
        self.assertEqual(html.count('data-media-index="1"'), 1)
        self.assertIn('<audio class="media-audio"', html)
        self.assertIn('src="media/voice.wav"', html)
        self.assertNotIn("media/voice.silk", html)
        self.assertIn("预览质量：缩略图", html)
        self.assertIn('<video class="media-video"', html)
        self.assertIn('href="media/readme.pdf"', html)

    def test_out_of_window_quote_shows_thumbnail_and_silk_playback_gap(self):
        html = render_conversation_html(
            account="primary",
            contact={"displayName": "引用联系人"},
            messages=[
                {
                    "serverId": 2,
                    "content": "回复",
                    "sender": {"displayName": "我", "role": "self"},
                    "quote": {"platformMessageId": "1"},
                }
            ],
            quoted_messages=[
                {
                    "serverId": 1,
                    "content": "窗口外的图片引用",
                    "sender": {"displayName": "对方", "role": "other"},
                    "media_manifest": [
                        {
                            "kind": "emoji",
                            "exportedPath": "media/quoted.gif",
                            "quality": "thumbnail",
                        },
                        {
                            "kind": "voice",
                            "exportedPath": "media/raw.silk",
                            "exportStatus": "available_local",
                        },
                    ],
                }
            ],
        )

        self.assertIn('class="quote-media-thumbnail"', html)
        self.assertIn('src="media/quoted.gif"', html)
        self.assertIn("预览质量：缩略图", html)
        self.assertIn("播放缺口", html)
        self.assertIn("下载原始语音", html)
        self.assertIn("media/raw.silk", html)
        self.assertNotIn('<audio class="media-audio" controls', html)

    def test_media_gap_and_quote_keep_binding_and_escape_quote_text(self):
        html = render_conversation_html(
            account="primary",
            contact={"displayName": "引用联系人"},
            messages=[
                {
                    "serverId": 8,
                    "createTime": 100,
                    "content": "原消息",
                    "sender": {"displayName": "对方", "role": "other"},
                },
                {
                    "serverId": 9,
                    "createTime": 110,
                    "content": "回复",
                    "sender": {"displayName": "我", "role": "self"},
                    "quote": {"platformMessageId": "8"},
                    "media_manifest": [
                        {
                            "kind": "image",
                            "exportStatus": "open_failed",
                            "exportGap": "resource_not_openable",
                        }
                    ],
                },
            ],
            quoted_messages=[
                {
                    "serverId": 8,
                    "content": "不会重复插入主消息",
                    "sender": {"displayName": "对方", "role": "other"},
                }
            ],
        )

        self.assertIn('class="quote"', html)
        self.assertIn('href="#message-0"', html)
        self.assertIn("查看原消息", html)
        self.assertIn("原消息", html)
        self.assertIn("媒体缺口：resource_not_openable", html)

        missing = render_conversation_html(
            account="primary",
            contact={"displayName": "引用联系人"},
            messages=[
                {
                    "serverId": 1,
                    "content": "<正文>",
                    "sender": {"displayName": "我", "role": "self"},
                    "quote": {"platformMessageId": "missing"},
                }
            ],
        )
        self.assertIn("[被引用消息不在当前导出范围]", missing)
        self.assertIn("引用缺口", missing)

    def test_metadata_and_local_path_are_escaped_without_remote_resources(self):
        html = render_conversation_html(
            account="primary",
            contact={"displayName": "<联系人>"},
            messages=[
                {
                    "localId": "a&b",
                    "content": "内容",
                    "sender": {"displayName": "我", "role": "self"},
                    "media_manifest": [
                        {"kind": "file", "exportedPath": "../outside.txt"},
                    ],
                }
            ],
            metadata={"source": "<local>", "timezone": "Asia/Shanghai"},
        )

        self.assertIn("&lt;联系人&gt;", html)
        self.assertIn("&lt;local&gt;", html)
        self.assertIn("媒体缺口：缺少导出路径", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("../outside.txt", html)


if __name__ == "__main__":
    unittest.main()
