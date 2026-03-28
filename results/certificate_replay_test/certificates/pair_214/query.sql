SELECT DISTINCT
  b.email
FROM PERSON AS A
JOIN PERSON AS B
  ON a.id <> b.id AND a.email = b.email