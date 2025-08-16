import os

def save_code_to_txt(file_paths, output_file='collected_code.txt'):
    with open(output_file, 'w', encoding='utf-8') as out_file:
        for path in file_paths:
            if not os.path.isfile(path):
                print(f"Файл не найден: {path}")
                continue
            try:
                with open(path, 'r', encoding='utf-8') as code_file:
                    code = code_file.read()
                    out_file.write(f"=== {os.path.basename(path)} ===\n")
                    out_file.write(code)
                    out_file.write("\n\n")  # отделяем следующий файл

            except Exception as e:
                print(f"Ошибка при чтении файла {path}: {e}")


# Пример использования
if __name__ == '__main__':
    paths = [
        "/Users/mchomak/Projects/notify_bot/db.py",
         "/Users/mchomak/Projects/notify_bot/config.py",
          "/Users/mchomak/Projects/notify_bot/fsm.py",
           "/Users/mchomak/Projects/notify_bot/handlers.py",
            "/Users/mchomak/Projects/notify_bot/main.py",
             "/Users/mchomak/Projects/notify_bot/setup_log.py",
              "/Users/mchomak/Projects/notify_bot/text.py",
    ]
    save_code_to_txt(paths)
    print("Код сохранен в collected_code.txt")
