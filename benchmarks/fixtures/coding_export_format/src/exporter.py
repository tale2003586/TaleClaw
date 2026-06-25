def to_lines(records):
    return [f"{item['id']}|{item['name']}" for item in records]
