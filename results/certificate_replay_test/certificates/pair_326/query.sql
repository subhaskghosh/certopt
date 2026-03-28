SELECT DISTINCT
  a.email
FROM PERSON AS A
JOIN PERSON AS B
  ON a.id <> b.id
WHERE
  a.email = b.email