SELECT DISTINCT
  a.email
FROM PERSON AS A
CROSS JOIN PERSON AS B
WHERE
  a.email = b.email AND a.id > b.id