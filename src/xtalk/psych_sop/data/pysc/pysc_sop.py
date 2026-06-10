# -*- coding: utf-8 -*-
"""
心理访谈标准流程类 PyscSOP vesion 0.1
- 该类用于实现心理访谈的标准操作流程（SOP），包括题目加载、用户交互、跳转逻辑、内容存储。
- 该类使用 JSON 文件作为题目数据源，支持动态加载和更新题目。
- 目前使用简单的评分函数模拟用户回答的评分逻辑，实际应用中可替换为更复杂的模型。
"""

import json
import os


def score_function(answer_text):
    """
    模拟评分函数，根据回答内容返回 '0'~'3' 字符串评分，用于跳转。
    实际中应调用 LLM 或其他模型进行自然语言打分。
    """
    # 固定的评分选项
    choices = {
        "资料不足": "0",
        "无或否": "1",
        "阈下": "2",
        "阈上或是": "3",
    }

    # 简单关键词匹配打分逻辑（可替换为真实模型）
    if "很严重" in answer_text or "经常" in answer_text or "总是" in answer_text:
        return choices["阈上或是"]
    elif "有一点" in answer_text or "偶尔" in answer_text:
        return choices["阈下"]
    elif "没有" in answer_text or "从未" in answer_text:
        return choices["无或否"]
    else:
        return choices["资料不足"]


class Question:
    def __init__(
        self, qid=None, text=None, tag=None, jump_table=None, copy_list=None, q=None
    ):
        """
        参数说明：
        - q: 可选，包含所有字段的字典
        - qid, text, tag, jump_table, copy_list: 可选，若为 None 则从 q 中取值
        """
        q = q or {}

        self.qid = qid if qid is not None else q.get("qid")
        self.text = text if text is not None else q.get("text")
        self.tag = tag if tag is not None else q.get("tag")
        self.jump_table = (
            jump_table if jump_table is not None else q.get("jump_table", {})
        )
        self.copy_list = copy_list if copy_list is not None else q.get("copy_list", [])

        self.answer_text = None
        self.score = None

    def ask(self):
        """
        提示用户回答，打分，记录。
        """
        print(f"{self.qid}: {self.text}")
        answer = input("你的回答: ")
        self.answer_text = answer
        self.score = score_function(answer)
        print(f"{self.score}: {answer}")

        return answer

    def get_next_qid(self):
        """
        根据当前得分跳转。如果无匹配跳转项，则返回 None。
        """
        if self.score is None:
            return None
        return self.jump_table.get(str(self.score), None)

    def copy_to(self, question_dict):
        """
        将当前题目的回答与得分复制到指定题目中。
        - question_dict: 所有题目对象的 dict（qid -> Question）
        """
        for target_qid in self.copy_list:
            target = question_dict.get(target_qid)
            if target:
                target.answer_text = self.answer_text
                target.score = self.score


class PyscSOP:
    def __init__(self):
        self.questions = {}  # qid -> Question 实例
        self.qid_order = []  # idx -> qid, 记录题目顺序
        self.qid_index_map = {}  # qid -> idx，方便顺序查找

    def load_questions(self, filename):
        """
        从 JSON 文件加载题目。
        - filename: str，文件路径
        """
        if self.questions:
            print("[info] 问卷新增题目")
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
        for q in data:
            question = Question(q=q)
            self.add_question(question)
        print(f"[info] 读取问卷 {filename} 成功，共 {len(data)} 道题目。")

    def add_question(self, question):
        """
        添加题目到问卷中。
        - question: Question 实例
        """
        self.qid_index_map[question.qid] = len(self.qid_order)
        self.qid_order.append(question.qid)
        self.questions[question.qid] = question

    def _get_next_qid_default(self, current_qid):
        idx = self.qid_index_map.get(current_qid)
        if idx + 1 < len(self.qid_order):
            return self.qid_order[idx + 1]
        return None

    def run(self):
        """
        执行问卷流程
        """
        if not self.qid_order:
            print("[info] 问卷为空")
            return

        current_qid = self.qid_order[0]
        while current_qid:
            # 获取当前题目
            # current_qid 一定在 self.qid_order 中，保证了question存在
            question = self.questions.get(current_qid)

            # 与用户交互
            # 1. 如果当前题目没有回答，则提示用户回答，并复制答案到指定题目
            # 2. 如果当前题目已经回答，则跳过提问
            if question.answer_text is None:
                question.ask()
                question.copy_to(self.questions)
            else:
                print(f"[info] {current_qid} 已有答案，跳过提问。")

            # 处理跳转
            # 1. 没有跳转 —— 默认下一题
            # 2. 跳转的题目不存在 —— 默认下一题
            # 3. 跳转的题目在当前问卷中 —— 进行跳转
            next_qid = question.get_next_qid()
            if next_qid is None:
                next_qid = self._get_next_qid_default(current_qid)
            elif next_qid not in self.qid_order:
                print(f"[info] 跳转的题目 {next_qid} 不在问卷中，(暂时)默认下一题。")
                next_qid = self._get_next_qid_default(current_qid)

            current_qid = next_qid

        print("\n[info] 问卷结束！回答记录如下：")
        self.show()

    def show(self):
        """
        显示回答记录
        """
        for qid in self.qid_order:
            q = self.questions[qid]
            print(f"{qid}: {q.text} -> 回答: {q.answer_text}, 得分: {q.score}")


if __name__ == "__main__":
    # fpc provide example
    interview1 = PyscSOP()
    interview1.load_questions("./pysc/SCID-5-S.json")
    interview1.run()
